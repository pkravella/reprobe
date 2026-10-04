"""Filesystem manifest diff -> authoritative file_write events.

strace can miss a write: an unparsed line, an interrupted syscall whose halves
were dropped, a path filtered out. The manifest diff cannot -- if the bytes
changed, the file is in here. So the `protected_write` check (Task 14) reads
these events, not strace's.

A write to an agent-state file is emitted twice, as a `file_write` and a
`memory_write`, for the same reason it is in the syscall observer: it both
changed the tree and changed how the agent behaves on a later turn (R7).
"""

from __future__ import annotations

from reprobe.observers.syscalls import _is_agent_state
from reprobe.sandbox import FsDiff
from reprobe.trace import Event


class FsDiffObserver:
    source = "fsdiff"

    def __init__(self, diff: FsDiff, *, ts: float) -> None:
        self._diff = diff
        self._ts = ts

    def events(self) -> list[Event]:
        out: list[Event] = []
        for op, paths in (
            ("created", self._diff.created),
            ("modified", self._diff.modified),
            ("deleted", self._diff.deleted),
        ):
            for path in paths:
                attrs = {"path": path, "op": op}
                out.append(Event(ts=self._ts, kind="file_write", source=self.source, attrs=attrs))
                if _is_agent_state(path):
                    out.append(
                        Event(ts=self._ts, kind="memory_write", source=self.source, attrs=attrs)
                    )
        return out
