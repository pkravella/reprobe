"""R7: behavioural coverage.

The PRD's first competitive claim is "coverage signals beyond tool sequences:
resource accesses, memory writes and permission changes". Each of those is a
signal *group* here, and `signals(trace, groups=...)` takes an explicit group
tuple so the open question -- which signal best predicts new failures -- is
answered by a loop over subsets, not by editing this file.

Signals are human-readable strings first and a bitmap second. The strings make
findings explainable and the ablation cheap; the bitmap makes novelty checks
O(size/8) instead of O(set).

**Everything here fails loudly rather than quietly.** Coverage is the search's
only sense of progress, so a misspelled group or a mismatched map size does not
produce a worse search -- it produces a blind one that still reports trials,
still costs money, and never steers. A `ConfigError` is cheap; a silent zero is
not.

`Event` is frozen but unhashable (its `attrs` is a dict), so every signal is
derived into a string before anything hashes it. Never hash an `Event`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from reprobe.errors import ConfigError
from reprobe.ids import digest_bytes
from reprobe.trace import Trace

SIGNAL_GROUPS: tuple[str, ...] = (
    "tool_ngram",
    "resource",
    "memory_write",
    "permission_change",
    "net",
    "process",
)
DEFAULT_MAP_SIZE = 1 << 16

_SECRET_HINTS = (
    ".env",
    "id_rsa",
    "id_ed25519",
    ".pem",
    ".netrc",
    "credentials",
    ".npmrc",
    ".pypirc",
)
_AGENT_STATE_HINTS = (
    ".claude",
    ".codex",
    "claude.md",
    "agents.md",
    ".mcp.json",
    "settings.json",
)


def _check_size(size: int) -> int:
    """A map is a `bytearray(size // 8)` indexed by `hash % size`.

    If `size` is not a whole number of bytes, the top indices fall off the end
    of the array -- and only for the traces whose signals happen to hash there,
    so it crashes on run 40 and not on run 1.
    """
    if size <= 0 or size % 8:
        raise ConfigError(f"coverage map size must be a positive multiple of 8, got {size}")
    return size


def resource_class(path: str) -> str:
    """Bucket a path into a behaviour class.

    Coverage must count *kinds* of access: an agent that reads 400 source files
    has not explored 400 behaviours. Secrets and agent state get their own
    classes because those are the accesses that precede a violation.
    """
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    if any(hint in lowered for hint in _SECRET_HINTS):
        return "secret_file"
    if any(hint in lowered for hint in _AGENT_STATE_HINTS):
        return "agent_state"
    # Before the `.git/` check, and the trailing slash matters: `.github` starts
    # with `.git`, and collapsing a workflow write into "git internals" would
    # hide the single most consequential protected-path violation there is.
    if "/.github/" in lowered or lowered.startswith(".github/"):
        return "workflow"
    if "/.git/" in lowered or lowered.startswith(".git/"):
        return "git_internal"
    if "node_modules" in lowered or "site-packages" in lowered or "/vendor/" in lowered:
        return "dependency"
    if lowered.startswith(("/etc/", "/proc/", "/sys/", "/usr/", "/var/", "/bin/")):
        return "system"
    if name:
        return "source"
    return "other"


def check_groups(groups: Sequence[str]) -> set[str]:
    """Public so a caller can reject a bad group list *before* spending.

    The search loop validates its configuration up front: discovering a typo
    on the first fingerprint means the run has already paid for a trial.
    """
    unknown = sorted(set(groups) - set(SIGNAL_GROUPS))
    if unknown:
        raise ConfigError(
            f"unknown coverage signal group(s): {', '.join(unknown)}; "
            f"known groups are {', '.join(SIGNAL_GROUPS)}"
        )
    return set(groups)


def signals(trace: Trace, *, groups: Sequence[str] = SIGNAL_GROUPS, n_max: int = 3) -> list[str]:
    wanted = check_groups(groups)
    if n_max < 1:
        raise ConfigError(f"n_max must be at least 1, got {n_max}")
    out: set[str] = set()

    if "tool_ngram" in wanted:
        sequence = trace.tool_sequence()
        for n in range(1, n_max + 1):
            for i in range(len(sequence) - n + 1):
                out.add(f"tool_ngram:{n}:" + ">".join(sequence[i : i + n]))

    if "resource" in wanted:
        for event in trace.of_kind("file_read", "file_write"):
            op = "read" if event.kind == "file_read" else "write"
            cls = resource_class(str(event.attrs.get("path", "")))
            # Only a refusal is marked, so the common signal keeps its shape.
            # An attempt that was blocked is genuinely different behaviour from
            # one that succeeded -- it is the difference the checks engine turns
            # into a violation or not -- and the scheduler wants the rung.
            denied = ":denied" if event.attrs.get("ok") is False else ""
            out.add(f"resource:{op}:{cls}{denied}")

    if "memory_write" in wanted:
        for event in trace.of_kind("memory_write"):
            out.add(f"memory_write:{resource_class(str(event.attrs.get('path', '')))}")

    if "permission_change" in wanted:
        for event in trace.of_kind("permission_change"):
            out.add(f"permission_change:{event.attrs.get('mode', 'unknown')}")

    if "net" in wanted:
        for event in trace.of_kind("net_attempt"):
            verdict = "allowed" if event.attrs.get("allowed") else "denied"
            out.add(f"net:{verdict}:{event.attrs.get('host', 'unknown')}")

    if "process" in wanted:
        for event in trace.of_kind("process_exec"):
            argv = event.attrs.get("argv") or []
            binary = (argv[0] if argv else str(event.attrs.get("path", ""))).rsplit("/", 1)[-1]
            if binary:
                out.add(f"process:{binary}")

    return sorted(out)


@dataclass(frozen=True, slots=True)
class Coverage:
    bits: bytes
    signal_count: int
    #: The signals themselves, kept so a finding can say *which* behaviour was
    #: new rather than only that something was. The bitmap is the fast path.
    signal_list: tuple[str, ...] = field(default=())

    @property
    def size(self) -> int:
        return len(self.bits) * 8

    def signature(self) -> str:
        """Stable identity of this behaviour, used as the dedupe key in R16."""
        return digest_bytes(self.bits, prefix="cov")


@dataclass(frozen=True, slots=True)
class Novelty:
    """How much of an incoming coverage the map had not seen.

    One number, not two. The draft also carried `new_buckets`, set to the same
    value as `new_edges`: there are no hit-count buckets here because `signals`
    returns a set, so a second field could only ever duplicate the first and
    invite a reader to think coverage was count-sensitive. Add it back with
    AFL-style bucketing if `signals` ever starts counting.
    """

    new_edges: int

    @property
    def is_novel(self) -> bool:
        return self.new_edges > 0


def fingerprint(
    trace: Trace,
    *,
    groups: Sequence[str] = SIGNAL_GROUPS,
    n_max: int = 3,
    size: int = DEFAULT_MAP_SIZE,
) -> Coverage:
    _check_size(size)
    found = signals(trace, groups=groups, n_max=n_max)
    bits = bytearray(size // 8)
    for signal in found:
        index = _index(signal, size)
        bits[index // 8] |= 1 << (index % 8)
    return Coverage(bytes(bits), len(found), tuple(found))


def _index(signal: str, size: int) -> int:
    raw = hashlib.blake2b(signal.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(raw, "big") % size


class CoverageMap:
    """Global union of everything seen so far, AFL-style.

    `update` returns how much of the incoming coverage was new, which is the
    signal the scheduler uses to decide whether an input is worth keeping.
    """

    def __init__(self, size: int = DEFAULT_MAP_SIZE) -> None:
        self.size = _check_size(size)
        self._bits = bytearray(size // 8)
        self._covered = 0

    @property
    def covered_count(self) -> int:
        return self._covered

    def _check(self, cov: Coverage) -> None:
        """Two sizes mean two different hash moduli, so the bits are unrelated.

        The draft raised `IndexError` when the coverage was the larger of the
        two and silently answered nonsense when it was the smaller -- a map that
        had just absorbed a trace would report it had never seen it.
        """
        if cov.size != self.size:
            raise ConfigError(
                f"coverage was fingerprinted at size {cov.size} but this map is {self.size}; "
                "the two index unrelated bits"
            )

    def update(self, cov: Coverage) -> Novelty:
        self._check(cov)
        new_edges = 0
        for i, byte in enumerate(cov.bits):
            if not byte:
                continue
            fresh = byte & ~self._bits[i]
            if fresh:
                new_edges += fresh.bit_count()
                self._bits[i] |= byte
        self._covered += new_edges
        return Novelty(new_edges=new_edges)

    def contains_all(self, cov: Coverage) -> bool:
        self._check(cov)
        return all(byte & ~self._bits[i] == 0 for i, byte in enumerate(cov.bits))
