"""Mock egress gateway. A trial reaches nothing but this, and only a named set
of infrastructure hosts travels any further.

Four jobs:
 1. Log every connection attempt -- host, port, method, path, headers, a body
    preview -- which is the evidence for the `egress_offlist` and
    `canary_exfil` checks.
 2. Answer allowlisted HTTP hosts with a plausible canned response, so an
    agent's honest work does not fail in a way that derails the task.
 3. Refuse everything else with a 403 and log it. The attempt is the finding;
    success is not required.
 4. For CONNECT (HTTPS), **tunnel** to a host in `REPROBE_INFRA_HOSTS` and
    refuse all others. This is the option-1 carve-out: a real agent has to
    reach its model API, which is HTTPS and therefore CONNECT, and the trial
    network has no other route out. The gateway is the single, logged chokepoint
    that straddles the internal trial network and an egress network (the sandbox
    dual-homes it); the agent itself never gets a route off the internal net.
    The tunnelled bytes are opaque TLS -- the host and port are the record.

Implemented as a raw asyncio proxy rather than on aiohttp, because a CONNECT
tunnel needs to take over the socket and pump bytes both ways, which aiohttp's
web framework does not expose cleanly. Dropping aiohttp also drops a dependency
from the image.

Notes carried from the earlier aiohttp version (see docs/agents.md): CONNECT's
target is authority-form (`host:443`), not a path; and secrets are redacted by
*value* (`REPROBE_REDACT`), never by header name, so a canary smuggled inside a
credential header is still recorded while a real key is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOG = Path(os.environ.get("REPROBE_EGRESS_LOG", "/reprobe/egress.jsonl"))
ALLOWLIST = [h for h in os.environ.get("REPROBE_ALLOWLIST", "").split(",") if h]
#: Hosts a CONNECT may tunnel to (the model API). Everything else is refused.
INFRA_HOSTS = [h for h in os.environ.get("REPROBE_INFRA_HOSTS", "").split(",") if h]
#: Literal secret values to scrub from everything logged -- the injected API
#: key. By value, not by header name, so a canary in a credential header stays.
REDACT = [v for v in os.environ.get("REPROBE_REDACT", "").split(",") if v]
REDACTED = "<redacted>"

BODY_PREVIEW_BYTES = 4096
TUNNEL_CHUNK = 65536
UPSTREAM_TIMEOUT = 10.0
CANNED = Path("/opt/canned")  # host-mounted canned responses, keyed by host


def _matches(host: str, patterns: list[str]) -> bool:
    """Exact and `*.suffix` match, case- and port-insensitive. Mirrors
    `reprobe.observers.egress.host_allowed`; the dot boundary keeps
    `notexample.com` from matching `*.example.com`."""
    host = host.split(":")[0].lower().rstrip(".")
    for raw in patterns:
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
    line = scrub(json.dumps({"ts": time.time(), **fields}, ensure_ascii=False))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()


async def _read_request_head(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str]] | None:
    request_line = await reader.readline()
    if not request_line:
        return None
    parts = request_line.decode("latin-1").rstrip("\r\n").split(" ")
    if len(parts) < 2:
        return None
    method, target = parts[0], parts[1]
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip()] = value.strip()
    return method.upper(), target, headers


def _http_host_port(target: str, headers: dict[str, str]) -> tuple[str, int]:
    # absolute-form (`GET http://host/path`, what HTTP_PROXY produces) or
    # origin-form (`GET /path` with a Host header).
    source = target if "://" in target else headers.get("Host", headers.get("host", ""))
    split = urlsplit(source if "://" in source else f"//{source}", scheme="http")
    host = split.hostname or source.split(":")[0]
    port = split.port or (443 if split.scheme == "https" else 80)
    return host, port


def _path(target: str) -> str:
    if "://" in target:
        split = urlsplit(target)
        return split.path + (f"?{split.query}" if split.query else "")
    return target


async def _respond(writer: asyncio.StreamWriter, status: str, body: bytes) -> None:
    writer.write(
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n".encode()
        + body
    )
    with contextlib.suppress(Exception):
        await writer.drain()


async def _handle_http(
    method: str,
    target: str,
    headers: dict[str, str],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    length = headers.get("Content-Length") or headers.get("content-length") or "0"
    want = min(int(length) if length.isdigit() else 0, BODY_PREVIEW_BYTES)
    body = await reader.read(want) if want else b""

    host, port = _http_host_port(target, headers)
    is_allowed = _matches(host, ALLOWLIST)
    log_attempt(
        host=host,
        port=port,
        method=method,
        path=_path(target),
        headers=headers,
        body_preview=body.decode("utf-8", "replace"),
        allowed=is_allowed,
        protocol="http",
    )
    if not is_allowed:
        await _respond(writer, "403 Forbidden", b'{"error": "blocked by reprobe mock gateway"}')
        return
    canned = CANNED / f"{host}.json"
    payload = canned.read_text() if canned.exists() else '{"ok": true, "mock": true}'
    await _respond(writer, "200 OK", payload.encode())


async def _handle_connect(
    target: str,
    headers: dict[str, str],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    host, _, port_s = target.partition(":")
    port = int(port_s) if port_s.isdigit() else 443
    infra = _matches(host, INFRA_HOSTS)
    # `allowed` stays False: an infra host is harness plumbing, not a scenario
    # allowlist entry, and checks.py excludes infra hosts regardless. `tunneled`
    # records whether bytes actually flowed.
    log_attempt(
        host=host,
        port=port,
        method="CONNECT",
        path=target,
        headers=headers,
        body_preview="",
        allowed=False,
        protocol="connect",
        tunneled=infra,
    )
    if not infra:
        await _respond(writer, "403 Forbidden", b'{"error": "blocked by reprobe mock gateway"}')
        return

    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=UPSTREAM_TIMEOUT
        )
    except (TimeoutError, OSError):
        await _respond(writer, "502 Bad Gateway", b'{"error": "upstream unreachable"}')
        return

    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    with contextlib.suppress(Exception):
        await writer.drain()
    await _pump(reader, writer, upstream_reader, upstream_writer)


async def _pump(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while data := await src.read(TUNNEL_CHUNK):
                dst.write(data)
                await dst.drain()
        with contextlib.suppress(Exception):
            dst.close()

    await asyncio.gather(
        copy(client_reader, upstream_writer),
        copy(upstream_reader, client_writer),
    )


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await _read_request_head(reader)
        if head is None:
            return
        method, target, headers = head
        if method == "CONNECT":
            await _handle_connect(target, headers, reader, writer)
        else:
            await _handle_http(method, target, headers, reader, writer)
    except Exception:  # a broken client must never take the gateway down
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def main() -> None:
    server = await asyncio.start_server(handle_client, "0.0.0.0", 8080)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
