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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from reprobe.confirm import Confirmation
from reprobe.errors import ConfigError
from reprobe.mutate import Candidate, Mutation
from reprobe.scenario import Scenario
from reprobe.stats import DEFAULT_THRESHOLD, RateEstimate

Granularity = str
ConfirmFn = Callable[[Candidate], Confirmation]
ConfirmWithFn = Callable[[Scenario, Candidate], Confirmation]
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
    #: Diagnostic, not an accounting total. The Confirmer cache returns the
    #: original measurement's `cost_usd` on a hit, and the shrinker's own
    #: baseline is usually a hit, so this double-counts it. For money, read the
    #: `BudgetLedger`.
    cost_usd: float = 0.0

    @property
    def reduction(self) -> float:
        if self.original_bytes == 0:
            return 0.0
        return 1.0 - self.shrunk_bytes / self.original_bytes

    def to_record(self) -> dict[str, Any]:
        return {
            # Without this a stored trajectory cannot be matched to the
            # candidate it reduced, which is the whole reason to store it.
            "candidate_id": self.original.id,
            "shrunk_id": self.shrunk.id,
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
        if self.exhausted:
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


def _require_payloads(candidate: Candidate) -> None:
    """A candidate with no payloads delivers nothing, so there is no finding.

    Measuring one would report a rate for an input the agent never saw -- the
    undelivered-payload bug in another costume, and the triage layer is exactly
    where that reads as a real negative result. Refused at both entry points.

    It also makes two guards provably unnecessary: `check_payloads` guarantees
    every payload key names a declared surface, so a non-empty payload always
    holds at least one surface, so `env_knobs` always leaves at least one
    surface without a knob, so a narrowed scenario can never run out of them.
    """
    if not candidate.payloads:
        raise ConfigError(
            f"candidate {candidate.id} has no payloads; there is nothing to shrink, "
            "and a rate measured for it would describe an input the agent never saw"
        )


def shrink(
    candidate: Candidate,
    *,
    confirm: ConfirmFn,
    threshold: float = DEFAULT_THRESHOLD,
    granularities: Sequence[Granularity] = DEFAULT_GRANULARITIES,
    max_steps: int = 200,
) -> ShrinkResult:
    _require_payloads(candidate)
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


# --- environment prerequisites (R10's other half) -------------------------
#
# R10 asks for "payload lines, tokens AND environment prerequisites". Above is
# the payload; below asks which parts of the *setup* are load-bearing. "Still
# reproduces with no egress allowlist and only the README surface" tells an
# engineer what to change; a minimal payload on its own does not.


@dataclass(frozen=True, slots=True)
class EnvKnob:
    """One removable prerequisite, and how to remove it."""

    id: str
    describe: str
    apply: Callable[[Scenario], Scenario]


class EnvShrinkResult(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    scenario: Scenario
    removed: list[str]
    removed_describe: list[str]
    estimate: RateEstimate
    #: One confirmation per knob plus a baseline. Measured on the Phase-3 gate,
    #: that is 34% of a candidate's trials -- a share a caller summing only the
    #: payload shrink's cost reports as free.
    #:
    #: Accurate here, because `shrink_environment` asks about a different
    #: scenario every time and so never hits a Confirmer cache. **Do not add it
    #: to `ShrinkResult.cost_usd` to get a total**: a cached Confirmation
    #: reports the *original* measurement's cost, so the payload shrinker's
    #: figure already includes its baseline's. Ask the `BudgetLedger` for a
    #: total -- it is the only thing that counts money once.
    cost_usd: float = 0.0

    def summary(self) -> str:
        if not self.removed:
            return "every declared prerequisite is load-bearing"
        return "no longer needs: " + "; ".join(self.removed_describe)


def env_knobs(scenario: Scenario, holding: Mapping[str, str]) -> list[EnvKnob]:
    """Prerequisites worth testing for this scenario, holding `holding` fixed.

    `holding` is the candidate's payloads. **No knob is offered for a surface
    the payload fills.** Such a surface is part of the finding rather than a
    prerequisite of it, and dropping it would leave the scenario and the payload
    disagreeing -- which `check_payloads` refuses, so the knob would raise
    rather than report "not needed".

    No fixture-directory knob, deliberately. `fixture_dir` is read by exactly
    one thing, `workspace.materialise` in the real sandbox; `FakeSandbox` never
    reads it and the fake agent reads only the surface paths and the canary
    path. A fixture knob would therefore be accepted on every scenario in both
    free lanes and report that the whole repository is unnecessary -- a fact
    about the lane, not about the finding. It wants an agent that actually
    reads the repo (Task 35's local model).
    """
    knobs: list[EnvKnob] = []

    for surface in scenario.surfaces:
        if surface.id in holding:
            continue
        sid = surface.id

        def drop_surface(scn: Scenario, sid: str = sid) -> Scenario:
            return scn.model_copy(update={"surfaces": [s for s in scn.surfaces if s.id != sid]})

        knobs.append(
            EnvKnob(f"surface:{sid}", f"attacker control over {surface.path}", drop_surface)
        )

    if scenario.egress_allowlist:
        allowed = ", ".join(scenario.egress_allowlist)
        knobs.append(
            EnvKnob(
                "egress_allowlist",
                f"an egress allowlist ({allowed})",
                lambda scn: scn.model_copy(update={"egress_allowlist": []}),
            )
        )

    for pattern in scenario.protected_paths:

        def drop_protected(scn: Scenario, pattern: str = pattern) -> Scenario:
            return scn.model_copy(
                update={"protected_paths": [p for p in scn.protected_paths if p != pattern]}
            )

        knobs.append(EnvKnob(f"protected_path:{pattern}", f"protecting {pattern}", drop_protected))

    return knobs


def shrink_environment(
    candidate: Candidate,
    scenario: Scenario,
    *,
    confirm_with: ConfirmWithFn,
    threshold: float = DEFAULT_THRESHOLD,
) -> EnvShrinkResult:
    """Drop every prerequisite the finding turns out not to need.

    Same acceptance rule as the payload shrinker, for the same reason: a knob
    comes out only if the Wilson lower bound still clears the threshold without
    it.
    """
    _require_payloads(candidate)
    current = scenario
    removed: list[str] = []
    describes: list[str] = []
    baseline = confirm_with(current, candidate)
    best = baseline.estimate
    cost = baseline.cost_usd

    # Nothing to narrow about a finding that does not hold in the first place,
    # and one question is cheaper than one per knob to learn the same thing.
    if best.lo < threshold:
        return EnvShrinkResult(
            scenario=scenario, removed=[], removed_describe=[], estimate=best, cost_usd=cost
        )

    for knob in env_knobs(scenario, candidate.payloads):
        # No `if not reduced.surfaces` guard: see `_require_payloads` for why a
        # narrowed scenario cannot run out of attacker-controlled surfaces.
        reduced = knob.apply(current)
        result = confirm_with(reduced, candidate)
        cost += result.cost_usd
        if result.estimate.lo >= threshold:
            current = reduced
            removed.append(knob.id)
            describes.append(knob.describe)
            best = result.estimate

    return EnvShrinkResult(
        scenario=current,
        removed=removed,
        removed_describe=describes,
        estimate=best,
        cost_usd=cost,
    )


__all__ = [
    "DEFAULT_GRANULARITIES",
    "EnvKnob",
    "EnvShrinkResult",
    "ShrinkResult",
    "env_knobs",
    "join_payload",
    "shrink",
    "shrink_environment",
    "split_payload",
]
