"""strace -f -yy -ttt output -> events.

This is the ground truth that does not depend on the agent telling us the truth.
We filter a deliberately small syscall set (see images/base/entrypoint.sh) so
the parser stays small and the overhead stays bounded.

Format notes, all taken from a real recording in `tests/data/strace-sample.log`
rather than from documentation, because the output format is the whole risk in
this module:

  * `-f` prefixes each line with a PID.
  * `-ttt` gives absolute epoch seconds.
  * `-yy` appends decoded fd/socket info in angle brackets. That decoration
    lands on the *first argument* too -- `openat(AT_FDCWD</workspace>, "/x", ...)`
    -- so the path is the quoted string, never the leading token. It also
    nests: a result can read `= 0</dev/null<char 1:3>>`.
  * Under `-f`, a syscall interrupted by another process's output is split in
    two. **The arguments are on the `<unfinished ...>` half and only the result
    is on the `<... name resumed>` half.** So the unfinished half is the one
    worth parsing, and a parser that requires a trailing `) = ret` drops both
    halves and loses the syscall entirely. In the recorded sample 84 of 309
    lines are halves like this, and one write -- `/workspace/CLAUDE.md`, an
    agent-state write that R7 names as a coverage signal -- appears *only* that
    way.
  * `-qq` does **not** suppress `--- SIGCHLD {...} ---` lines.
  * Non-ASCII arguments are escaped as octal of the UTF-8 bytes
    (`caf\\303\\251` for `café`), which needs a latin-1 round trip to decode;
    the obvious `unicode_escape` alone yields mojibake.
  * coreutils uses the `*at` variants: `fchmodat`, `unlinkat`, `renameat2`.
    There is no bare `chmod` line in a real recording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from reprobe.trace import Event, EventKind

_RESULT = r"=\s*(?P<ret>-?\d+|\?)(?P<retdec>\S*)(?:\s+(?P<errno>E[A-Z][A-Z0-9]*))?"

_COMPLETE = re.compile(
    r"^(?P<pid>\d+)\s+(?P<ts>\d+\.\d+)\s+(?P<name>\w+)\((?P<args>.*)\)\s*" + _RESULT
)
_UNFINISHED = re.compile(
    r"^(?P<pid>\d+)\s+(?P<ts>\d+\.\d+)\s+(?P<name>\w+)\((?P<args>.*?)\s*<unfinished \.\.\.>\s*$"
)
_RESUMED = re.compile(
    r"^(?P<pid>\d+)\s+(?P<ts>\d+\.\d+)\s+<\.\.\.\s+(?P<name>\w+)\s+resumed>\)?\s*" + _RESULT
)

_STR_ARG = re.compile(r'"((?:[^"\\]|\\.)*)"')
_INET = re.compile(
    r"sa_family=AF_INET6?.*?sin6?_port=htons\((?P<port>\d+)\).*?"
    r'inet6?_(?:addr|pton)\([^"]*"(?P<addr>[^"]+)"'
)
_MODE = re.compile(r"0[0-7]{3,4}")
_WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND")

# Writes here change how the agent behaves on later turns: a coverage signal the
# PRD calls out explicitly (R7: "memory writes, permission changes").
AGENT_STATE_PATTERNS: tuple[str, ...] = (
    "/.claude/",
    "/.codex/",
    "/.config/",
    "CLAUDE.md",
    "AGENTS.md",
    ".mcp.json",
    "settings.json",
    "settings.local.json",
)

_EXEC = ("execve", "execveat")
_OPEN = ("open", "openat")
_CHMOD = ("chmod", "fchmod", "fchmodat")
_REMOVE = ("unlink", "unlinkat", "rename", "renameat", "renameat2")


def _is_agent_state(path: str) -> bool:
    return any(pattern in path for pattern in AGENT_STATE_PATTERNS)


def _decode(raw: str) -> str:
    r"""Decode strace's escaping of a string argument.

    strace writes non-ASCII as octal escapes of the UTF-8 bytes, so
    `caf\303\251` is `café`. `raw.encode().decode("unicode_escape")` turns each
    octal escape into the *code point* of that byte and yields `cafÃ©`; the
    bytes have to be put back through latin-1 before being read as UTF-8.
    """
    try:
        as_bytes = raw.encode("latin-1", "backslashreplace").decode("unicode_escape")
        return as_bytes.encode("latin-1").decode("utf-8", "replace")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return raw


@dataclass(frozen=True)
class _Call:
    """One syscall, however many lines it took to print."""

    pid: int
    ts: float
    name: str
    args: str
    ret: str | None = None
    errno: str | None = None

    @property
    def ok(self) -> bool | None:
        """True, False, or None when the result was never printed.

        None matters: a trial killed on timeout truncates the log mid-syscall,
        and "we do not know" is not the same as "it failed".
        """
        if self.ret is None:
            return None
        if self.errno is not None:
            return False
        if self.ret == "?":
            return None
        return int(self.ret) >= 0

    def resolved(self, ret: str | None, errno: str | None) -> _Call:
        return _Call(self.pid, self.ts, self.name, self.args, ret, errno)


def _match_call(line: str) -> _Call | None:
    text = line.strip()
    if not text:
        return None
    complete = _COMPLETE.match(text)
    if complete is not None:
        return _Call(
            pid=int(complete["pid"]),
            ts=float(complete["ts"]),
            name=complete["name"],
            args=complete["args"],
            ret=complete["ret"],
            errno=complete["errno"],
        )
    unfinished = _UNFINISHED.match(text)
    if unfinished is not None:
        return _Call(
            pid=int(unfinished["pid"]),
            ts=float(unfinished["ts"]),
            name=unfinished["name"],
            args=unfinished["args"],
        )
    return None


def _events_for(call: _Call) -> list[Event]:
    base: dict[str, Any] = {"pid": call.pid, "ok": call.ok}
    if call.errno:
        base["errno"] = call.errno
    strings = [_decode(s) for s in _STR_ARG.findall(call.args)]

    def event(kind: EventKind, **attrs: Any) -> Event:
        return Event(ts=call.ts, kind=kind, source="strace", attrs={**base, **attrs})

    if call.name in _EXEC:
        argv = strings[1:] or strings[:1]
        return [event("process_exec", path=strings[0] if strings else "", argv=argv)]

    if call.name in _OPEN:
        if not strings:
            return []
        path = strings[-1]
        writing = any(flag in call.args for flag in _WRITE_FLAGS)
        events = [
            event(
                "file_write" if writing else "file_read",
                path=path,
                flags=_flags(call.args),
            )
        ]
        # Only a write changes the agent's future behaviour; reading its own
        # instructions is ordinary.
        if writing and _is_agent_state(path):
            events.append(event("memory_write", path=path))
        return events

    if call.name == "connect":
        inet = _INET.search(call.args)
        if inet is None:
            # AF_UNIX and friends. Not egress, and emitting them as net_attempt
            # would put filesystem paths in a host field and pollute the
            # allowlist checks.
            return []
        return [event("net_attempt", host=inet["addr"], port=int(inet["port"]), protocol="tcp")]

    if call.name in _CHMOD:
        mode = _MODE.search(call.args)
        return [
            event(
                "permission_change",
                path=strings[-1] if strings else "",
                mode=mode.group(0) if mode else "",
            )
        ]

    if call.name in _REMOVE:
        attrs: dict[str, Any] = {"path": strings[-1] if strings else "", "op": call.name}
        if len(strings) > 1:
            # A rename's destination is the write, but the source stops
            # existing, so moving a protected file away is also a change to it.
            attrs["src"] = strings[0]
        return [event("file_write", **attrs)]

    return []


def _flags(args: str) -> str:
    found = [flag for flag in _WRITE_FLAGS if flag in args]
    return "|".join(found) if found else "O_RDONLY"


def parse_line(line: str) -> list[Event]:
    """One strace line -> its events. Empty for noise or an unparseable line.

    A list, not a single event, because one `openat` of an agent-state file is
    both a `file_write` and a `memory_write`.
    """
    call = _match_call(line)
    return [] if call is None else _events_for(call)


class SyscallObserver:
    source = "strace"

    def __init__(
        self,
        log_text: str,
        *,
        workspace: str = "/workspace",
        home: str = "/home/agent",
    ) -> None:
        self._log_text = log_text
        self.workspace = workspace
        self.home = home

    def events(self) -> list[Event]:
        calls = self._calls()
        events = [e for call in calls for e in _events_for(call)]
        # Sorted by the timestamp of the line the arguments came from. An
        # interrupted syscall is emitted when its result arrives, which is out
        # of order relative to where it started.
        return sorted(events, key=lambda e: e.ts)

    def _calls(self) -> list[_Call]:
        """Fold the log's lines into one entry per syscall.

        A process has at most one syscall in flight, so a pending unfinished
        call is keyed by pid alone.
        """
        pending: dict[int, _Call] = {}
        calls: list[_Call] = []
        for line in self._log_text.splitlines():
            text = line.strip()
            if not text:
                continue
            resumed = _RESUMED.match(text)
            if resumed is not None:
                started = pending.pop(int(resumed["pid"]), None)
                if started is not None:
                    calls.append(started.resolved(resumed["ret"], resumed["errno"]))
                continue
            call = _match_call(text)
            if call is None:
                continue  # signal lines, attach/detach notices, torn final line
            if call.ret is None:
                pending[call.pid] = call
            else:
                calls.append(call)
        # Whatever never resumed still happened, and `ok` stays None for it.
        calls.extend(pending.values())
        return calls
