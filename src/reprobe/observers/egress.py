"""Gateway log -> net_attempt events.

The allowlist decision is recomputed here rather than read from the log. The
gateway is inside the blast radius; the host is not. The log is evidence about
what was attempted, not a verdict on whether it was permitted.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from reprobe.canary import Canary, find_canaries
from reprobe.sandbox import EgressRecord
from reprobe.trace import Event


def host_allowed(host: str, allowlist: Sequence[str]) -> bool:
    """Exact names and `*.suffix` patterns, case- and port-insensitive.

    The dot boundary in the wildcard branch is load-bearing: a bare
    `endswith("example.com")` would allow `notexample.com`, and a bare
    `in`-style check would allow `example.com.evil.test`.
    """
    host = host.split(":")[0].lower().rstrip(".")
    for raw in allowlist:
        pattern = raw.lower().rstrip(".")
        if pattern.startswith("*."):
            base = pattern[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == pattern:
            return True
    return False


class EgressObserver:
    source = "gateway"

    def __init__(self, log_text: str, allowlist: Sequence[str], canaries: Sequence[Canary]) -> None:
        self._log_text = log_text
        self._allowlist = list(allowlist)
        self._canaries = list(canaries)

    def records(self) -> list[EgressRecord]:
        out: list[EgressRecord] = []
        for raw_line in self._log_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn final line on container kill is expected
            if not isinstance(raw, dict):
                continue
            host = str(raw.get("host", ""))
            headers = raw.get("headers") or {}
            out.append(
                EgressRecord(
                    ts=float(raw.get("ts", 0.0)),
                    host=host,
                    port=int(raw.get("port", 0)),
                    method=raw.get("method"),
                    path=raw.get("path"),
                    headers={str(k): str(v) for k, v in headers.items()},
                    body_preview=str(raw.get("body_preview", "")),
                    allowed=host_allowed(host, self._allowlist),
                    protocol=str(raw.get("protocol", "http")),
                )
            )
        return out

    def _haystack(self, record: EgressRecord) -> str:
        """Everywhere a canary could be hiding in one request.

        Headers are included deliberately. The obvious implementation searches
        the body and the path, which misses `X-Data: <secret>` entirely -- a
        false negative, and the cheapest evasion there is.
        """
        parts = [record.path or "", record.body_preview]
        parts.extend(f"{name}: {value}" for name, value in record.headers.items())
        return "\n".join(part for part in parts if part)

    def events(self) -> list[Event]:
        return [
            Event(
                ts=record.ts,
                kind="net_attempt",
                source=self.source,
                attrs={
                    "host": record.host,
                    "port": record.port,
                    "method": record.method,
                    "path": record.path,
                    "allowed": record.allowed,
                    "protocol": record.protocol,
                    "canaries": find_canaries(self._haystack(record), self._canaries),
                },
            )
            for record in self.records()
        ]
