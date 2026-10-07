"""R10: delta debugging when the oracle is a coin flip.

Classic ddmin assumes a deterministic test. An agent is not deterministic, so
every "does it still fail?" question is a statistical one, answered by the
Confirmer. The acceptance rule is the whole trick:

    accept a cut  <=>  wilson_lower_bound(reduced) >= threshold

Using the point estimate instead would accept cuts that merely got lucky, and
the exported test would then flake -- which is precisely the failure mode the
PRD exists to eliminate. The gap is not academic: a payload measured at 10 of
20 trials has a point estimate of 0.50, comfortably clear of a 0.30 threshold,
and a lower bound of 0.2993, just under it. Point estimates keep that cut.

Note which way the error runs. The lower-bound rule is conservative -- it
rejects genuine cuts whose rate sits near the threshold (measured in Task 22:
only 31% of true-0.35 payloads are accepted). So a shrunk payload comes out
*larger* than strictly necessary rather than a reproduction rate coming out
overstated. That is the right way round for an exported regression test, and it
means the PRD's 60% reduction target is harder than it looks, not easier.

Cost: each candidate cut costs 5-40 agent trials, so the Confirmer's cache and
the coarse-to-fine ordering matter. Surfaces first (one question can delete
kilobytes), then blocks, then lines, then tokens.

**One piece of state, deliberately.** An earlier draft tracked the best
candidate in a closure variable and the current candidate in a local, and the
two disagreed: cuts were accepted, 96% of the payload was removed, and the
mutation lineage came back empty because the undecorated local was assigned
over the decorated closure variable at the end of every pass. A reported rate
that belongs to a different payload than the reported one is the worst bug this
module could have, so candidate and estimate move together or not at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel

from reprobe.confirm import Confirmation
from reprobe.mutate import Candidate, Mutation
from reprobe.stats import DEFAULT_THRESHOLD, RateEstimate

Granularity = str
ConfirmFn = Callable[[Candidate], Confirmation]
DEFAULT_GRANULARITIES: tuple[Granularity, ...] = ("surface", "block", "line", "token")
_SEPARATORS: dict[Granularity, str] = {"block": "\n\n", "line": "\n", "token": " "}


class ShrinkResult(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    original: Candidate
    shrunk: Candidate
    original_bytes: int
    shrunk_bytes: int
    estimate: RateEstimate
    steps: int = 0
    cost_usd: float = 0.0

    @property
    def reduction(self) -> float:
        if self.original_bytes == 0:
            return 0.0
        return 1.0 - self.shrunk_bytes / self.original_bytes

    def to_record(self) -> dict[str, Any]:
        return {
            "original_bytes": self.original_bytes,
            "shrunk_bytes": self.shrunk_bytes,
            "reduction": self.reduction,
            "payloads": self.shrunk.payloads,
            "rate_point": self.estimate.point,
            "rate_lo": self.estimate.lo,
            "rate_hi": self.estimate.hi,
            "steps": self.steps,
            "cost_usd": self.cost_usd,
        }


def split_payload(text: str, granularity: Granularity) -> list[str]:
    try:
        return text.split(_SEPARATORS[granularity])
    except KeyError:
        raise ValueError(f"not a splittable granularity: {granularity!r}") from None


def join_payload(parts: Sequence[str], granularity: Granularity) -> str:
    return _SEPARATORS[granularity].join(parts)


class _Run:
    """The shrink in progress: the oracle, the budget, and the single best state.

    `candidate` and `estimate` are only ever replaced together, in `try_cut`.
    Nothing outside this class may assign either.
    """

    def __init__(self, candidate: Candidate, confirm: ConfirmFn, threshold: float, cap: int):
        self._confirm = confirm
        self._threshold = threshold
        self._cap = cap
        self.candidate = candidate
        baseline = confirm(candidate)
        self.estimate = baseline.estimate
        self.steps = 1
        self.cost = baseline.cost_usd

    @property
    def exhausted(self) -> bool:
        """The cap is checked here and nowhere else.

        `try_cut` is the only thing that spends, so it is the only thing that
        needs to stop. Guarding the loops as well was redundant, and worse: it
        meant the check inside `try_cut` could never fire, so the one place the
        budget is actually enforced went untested.
        """
        return self.steps >= self._cap

    @property
    def holds(self) -> bool:
        """Does the finding clear the threshold at all? If not, nothing to cut."""
        return self.estimate.lo >= self._threshold

    def try_cut(self, payloads: dict[str, str], granularity: Granularity) -> bool:
        """Ask the oracle about `payloads`; adopt them only if the bound holds."""
        if self.exhausted or not payloads:
            return False
        reduced = Candidate.rebuild(
            payloads, lineage=self.candidate.lineage, seed_ids=self.candidate.seed_ids
        )
        self.steps += 1
        result = self._confirm(reduced)
        self.cost += result.cost_usd
        if result.estimate.lo < self._threshold:
            return False
        self.candidate = reduced.derive(
            payloads,
            Mutation(
                mutator="shrink",
                params={"granularity": granularity, "bytes": reduced.byte_size},
            ),
        )
        self.estimate = result.estimate
        return True

    def result(self, original: Candidate) -> ShrinkResult:
        return ShrinkResult(
            original=original,
            shrunk=self.candidate,
            original_bytes=original.byte_size,
            shrunk_bytes=self.candidate.byte_size,
            estimate=self.estimate,
            steps=self.steps,
            cost_usd=self.cost,
        )


def shrink(
    candidate: Candidate,
    *,
    confirm: ConfirmFn,
    threshold: float = DEFAULT_THRESHOLD,
    granularities: Sequence[Granularity] = DEFAULT_GRANULARITIES,
    max_steps: int = 200,
) -> ShrinkResult:
    run = _Run(candidate, confirm, threshold, max_steps)

    # The finding does not hold up at the threshold in the first place, so there
    # is nothing to reduce. Return it untouched for the caller to drop honestly
    # -- shrinking an unreproducible candidate would just find the smallest
    # payload that also fails to reproduce.
    if not run.holds:
        return run.result(candidate)

    for granularity in granularities:
        if granularity == "surface":
            _drop_dead_surfaces(run)
        else:
            for sid in sorted(run.candidate.payloads):
                _shrink_one_surface(run, sid, granularity)

    return run.result(candidate)


def _drop_dead_surfaces(run: _Run) -> None:
    """Try removing whole surfaces. The biggest single win available, so first."""
    progressed = True
    while progressed:
        progressed = False
        for sid in sorted(run.candidate.payloads):
            if len(run.candidate.payloads) == 1:
                break
            kept = {k: v for k, v in run.candidate.payloads.items() if k != sid}
            if run.try_cut(kept, "surface"):
                progressed = True
                break  # the surface list changed; re-derive it


def _shrink_one_surface(run: _Run, sid: str, granularity: Granularity) -> None:
    """ddmin over one surface's parts at one granularity.

    The pass restarts after every accepted cut rather than carrying an index
    across a list that just changed length. It costs a few re-tested chunks and
    removes the whole class of off-by-one that indexing into a mutated list
    invites.
    """
    parts = split_payload(run.candidate.payloads[sid], granularity)
    n = 2
    while len(parts) >= 2:
        chunk = max(1, len(parts) // n)
        accepted = False
        index = 0
        while index < len(parts):
            trimmed = parts[:index] + parts[index + chunk :]
            # Never cut a surface down to nothing: that is the environment
            # shrinker's job (Task 24), and an empty payload is a different
            # finding, not a smaller one.
            if not any(part.strip() for part in trimmed):
                index += chunk
                continue
            payloads = dict(run.candidate.payloads)
            payloads[sid] = join_payload(trimmed, granularity)
            if run.try_cut(payloads, granularity):
                parts = trimmed
                accepted = True
                n = 2  # the payload is smaller; try coarse cuts again
                break
            index += chunk
        if not accepted:
            if chunk == 1:
                return  # finest cuts all rejected; this granularity is done
            n = min(len(parts), n * 2)


__all__ = [
    "DEFAULT_GRANULARITIES",
    "ShrinkResult",
    "join_payload",
    "shrink",
    "split_payload",
]
