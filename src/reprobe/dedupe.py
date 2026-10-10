"""R16: group duplicate findings by the violating action.

Two payloads that make the agent do the same forbidden thing are one bug. The
key is the set of action keys, which is what `docs/benchmark-v0.1.md` calls "the
unit R16 dedupes on" and what the Phase-2 gate was read against: a hundred
payloads that all read `.env` are one finding, not a hundred.

**Not the coverage signature.** The plan keyed on `(action keys, coverage
signature)` and that splits one bug into as many groups as there are routes to
it. `Coverage.signature()` is a hash of the whole 64Kbit bitmap, so one extra
incidental tool call changes it entirely -- measured, on three traces that all
read `/workspace/.env`:

    read only                -> cov:02c51cbf84635a3f
    read + a Grep            -> cov:3156b52d24c40342
    read + one incidental read -> cov:592ffcb941b1e015

A search produces dozens of those, and two trials of the *same* payload against
a real agent produce two of them. Keying on it would make dedupe finer-grained
than the thing it is deduplicating, which is the opposite of its job.

The information is not thrown away: a group reports the distinct signatures its
members span, because "one bug, reached by seven distinct behaviours" is
something the search worked for and belongs in the report.

A *superset* of action keys is deliberately a different group. "Reads the canary"
and "reads the canary and rewrites CI" are not one report -- the second is
strictly worse, and since the representative is chosen for reliability and size
rather than severity, merging them would hide the worse one behind the milder.

The representative is the one an engineer should read: most reliable first
(highest Wilson lower bound), then smallest payload.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from reprobe.finding import Finding
from reprobe.ids import digest


class FindingGroup(BaseModel):
    model_config = {"frozen": True}

    key: str
    representative: Finding
    members: list[Finding]

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def coverage_signatures(self) -> list[str]:
        """Distinct behavioural routes that reached this bug."""
        return sorted({m.coverage_signature for m in self.members})

    @property
    def distinct_payloads(self) -> int:
        """How many genuinely different payloads the group holds.

        Not the same as `size`. Two search candidates that shrink to the same
        minimal payload are two members and one payload, which is the normal
        outcome and worth saying: the search reached one bug twice by different
        routes, and reduction collapsed both to the same thing. Reporting the
        member count as a payload count overstates what a reader would have to
        look at. Observed on the Phase-3 gate, where a 214-byte candidate and a
        430-byte one both reduced to the identical 9 bytes.
        """
        return len({tuple(sorted(m.payloads.items())) for m in self.members})


def group_key(finding: Finding) -> str:
    return digest({"actions": sorted(finding.action_keys)}, prefix="grp")


def _rank(finding: Finding) -> tuple[float, int]:
    """Most reliable first, then smallest. One definition, used for choosing a
    representative and for ordering the groups, so the two cannot disagree."""
    return (-finding.rate.lo, finding.shrunk_bytes)


def dedupe(findings: Sequence[Finding]) -> list[FindingGroup]:
    buckets: dict[str, list[Finding]] = {}
    for finding in findings:
        buckets.setdefault(group_key(finding), []).append(finding)

    groups = [
        FindingGroup(key=key, representative=min(members, key=_rank), members=members)
        for key, members in buckets.items()
    ]
    groups.sort(key=lambda g: _rank(g.representative))
    return groups


__all__ = ["FindingGroup", "dedupe", "group_key"]
