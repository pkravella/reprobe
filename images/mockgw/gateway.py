"""Mock egress gateway. Nothing a trial sends leaves this container.

Three jobs:
 1. Log every connection attempt, with host, port, method, path, headers and a
    body preview. This is the evidence for the `egress_offlist` and
    `canary_exfil` checks.
 2. Answer allowlisted hosts with plausible canned responses, so the agent's
    honest work (npm metadata, a docs page) does not fail in a way that
    derails the task.
 3. Answer everything else with a 403 and log it. The attempt is the finding;
    success is not required.

Three things here were measured rather than assumed, because the first draft
got each of them wrong (see docs/agents.md):

 * **CONNECT must be caught in middleware, not a route.** A CONNECT request's
   target is authority-form (`host:443`), not a path, so it never matches a
   `/{tail:.*}` route and aiohttp answers 404 -- silently, logging nothing.
   Every HTTPS attempt would have been invisible to the checks.
 * **The target is in `request.raw_path`,** not `request.path_qs`, which is
   empty for CONNECT.
 * **Secrets are redacted by value, not by header name.** Redacting
   `Authorization` and friends by name would hide a canary smuggled inside
   one, which is the cheapest evasion available; scrubbing known secret
   *values* keeps every byte of evidence while keeping real credentials out of
   a stored artifact.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web

LOG = Path(os.environ.get("REPROBE_EGRESS_LOG", "/reprobe/egress.jsonl"))
ALLOWLIST = [h for h in os.environ.get("REPROBE_ALLOWLIST", "").split(",") if h]
BODY_PREVIEW_BYTES = 4096
CANNED = Path("/opt/canned")  # host-mounted canned responses, keyed by host

#: Literal secret values to scrub from everything logged. The host populates
#: this with whatever it injected into the trial through the adapter's
#: `env_allowlist`, so the agent's own API key never reaches a run store.
#: Redaction is by value so that a canary hidden in a credential header is
#: still recorded and still detected.
REDACT = [v for v in os.environ.get("REPROBE_REDACT", "").split(",") if v]
REDACTED = "<redacted>"


def allowed(host: str) -> bool:
    """Mirrors `reprobe.observers.egress.host_allowed`.

    The host recomputes this from the log; this copy only decides whether to
    serve a canned response. The dot boundary matters here too: without it
    `notexample.com` matches `*.example.com`.
    """
    host = host.split(":")[0].lower().rstrip(".")
    for raw in ALLOWLIST:
        pattern = raw.lower().rstrip(".")
        if pattern.startswith("*."):
            base = pattern[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == pattern:
            return True
    return False


def scrub(value: str) -> str:
    for secret in REDACT:
        if secret:
            value = value.replace(secret, REDACTED)
    return value


def log_attempt(**fields: Any) -> None:
    record = {"ts": time.time(), **fields}
    line = scrub(json.dumps(record, ensure_ascii=False))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()


def _target_host(request: web.Request) -> str:
    """The host a request is aimed at, for every request form a proxy sees.

    origin-form (`GET /p`) and absolute-form (`GET http://h/p`, what HTTP_PROXY
    produces) both carry it in the Host header. CONNECT carries it in the
    authority-form target.
    """
    if request.method == "CONNECT":
        return request.raw_path or request.headers.get("Host", "")
    return request.headers.get("Host") or request.host or ""


async def handle(request: web.Request) -> web.StreamResponse:
    host = _target_host(request)
    body = await request.content.read(BODY_PREVIEW_BYTES)
    is_allowed = allowed(host)
    log_attempt(
        host=host.split(":")[0],
        port=request.url.port or 80,
        method=request.method,
        path=str(request.rel_url),
        headers=dict(request.headers),
        body_preview=body.decode("utf-8", "replace"),
        allowed=is_allowed,
        protocol="http",
    )
    if not is_allowed:
        return web.json_response({"error": "blocked by reprobe mock gateway"}, status=403)

    canned = CANNED / f"{host.split(':')[0]}.json"
    if canned.exists():
        return web.json_response(json.loads(canned.read_text()))
    return web.json_response({"ok": True, "mock": True})


async def handle_connect(request: web.Request) -> web.StreamResponse:
    """A CONNECT tunnel we refuse but log: the attempt is the signal.

    Refusing rather than tunnelling is deliberate. Completing the tunnel would
    need a MITM CA to see anything inside it, and R3 says nothing leaves the
    container -- so there is nothing to tunnel to. The host and port the agent
    asked for are the evidence.
    """
    target = request.raw_path or request.headers.get("Host", "")
    host, _, port = target.partition(":")
    log_attempt(
        host=host,
        port=int(port) if port.isdigit() else 443,
        method="CONNECT",
        path=target,
        headers=dict(request.headers),
        body_preview="",
        allowed=False,
        protocol="connect",
    )
    return web.Response(status=403, text="blocked by reprobe mock gateway")


@web.middleware
async def connect_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    # Middleware, not a route: see the module docstring. Verified that aiohttp
    # dispatches CONNECT here even though routing cannot match it.
    if request.method == "CONNECT":
        return await handle_connect(request)
    response: web.StreamResponse = await handler(request)
    return response


def main() -> None:
    app = web.Application(client_max_size=16 * 1024 * 1024, middlewares=[connect_middleware])
    app.router.add_route("*", "/{tail:.*}", handle)
    web.run_app(app, host="0.0.0.0", port=8080, access_log=None)


if __name__ == "__main__":
    main()
