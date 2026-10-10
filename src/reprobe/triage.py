"""The bottom row of the PRD's architecture diagram.

For every candidate the search found:
    confirm -> (drop if the lower bound is under threshold)
            -> shrink payload
            -> shrink environment
            -> re-confirm the shrunk form, in the narrowed environment,
               with fresh trials
            -> record a Finding

**Why the final re-confirmation needs its own Confirmer.** Every rate measured
during shrinking was used to *accept or reject a cut*, so it is conditioned on
having cleared the threshold -- quoting it as the finding's rate reports a
number selected for being high. The final measurement must therefore be
independent of every decision it is reporting on. A fresh `Confirmer` is what
makes it so: the cache deliberately ignores the seed (so the shrinker does not
repay for a payload it has already measured), which means re-asking the same
Confirmer about the shrunk payload returns the stored answer and runs nothing at
all.

**And it runs against the narrowed scenario.** The finding records the narrowed
scenario's hash, so that is the environment its rate has to have been measured
in. Measuring in the original and recording the narrowed hash is the same class
of mistake as a shrinker reporting a rate for a candidate it did not keep, and
it breaks the replay guarantee the hash exists for: an exported test would set
up one environment and quote a number from another.

`tests/unit/test_triage.py` pins both as one invariant -- every trial backing a
finding's rate ran against the scenario hash the finding claims, and the
successes it reports are the successes those trials produced.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from reprobe.agents.base import AgentMeta
from reprobe.budget import BudgetCaps, BudgetLedger
from reprobe.confirm import Confirmation, Confirmer
from reprobe.dedupe import FindingGroup, dedupe
from reprobe.errors import ConfigError
from reprobe.finding import Finding
from reprobe.mutate import Candidate, Mutation
from reprobe.sandbox import SandboxProtocol
from reprobe.scenario import Scenario
from reprobe.shrink import shrink, shrink_environment
from reprobe.stats import DEFAULT_THRESHOLD, RateEstimate
from reprobe.store import RunStore


class TriageConfig(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    threshold: float = DEFAULT_THRESHOLD
    min_trials: int = 5
    max_trials: int = 40
    caps: BudgetCaps
    shrink_env: bool = True
    max_shrink_steps: int = 120
    target_width: float = 0.25
    #: Reaches `run_trial`, which turns it into the fake agent's environment.
    #: Without it the free lane's agent does nothing, every trial comes back
    #: clean, and triage reports "rate 0.00, decisively below" for every
    #: candidate -- a flat zero that reads exactly like an agent that resisted.
    #: The CLI had the flag and passed it nowhere; `test_the_agent_profile
    #: _reaches_the_trial` is the guard.
    agent_profile: str | None = None
    #: An agent's own model API is not an egress violation. Omitted, a
    #: tunnelled call to it would be counted as one and every rate would be
    #: measured against the wrong verdict.
    infra_hosts: tuple[str, ...] = ()


class DroppedCandidate(BaseModel):
    """A candidate that did not survive triage, kept rather than discarded.

    A 10%-reproducible failure is not a finding, but it is still interesting to
    a researcher, and silently dropping it would make a triage run that found
    nothing indistinguishable from one that was never wired up.
    """

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    candidate_id: str
    reason: str
    rate: RateEstimate


class TriageReport(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    run_id: str
    findings: list[Finding] = Field(default_factory=list)
    groups: list[FindingGroup] = Field(default_factory=list)
    dropped: list[DroppedCandidate] = Field(default_factory=list)
    cost_usd: float = 0.0
    median_reduction: float = 0.0
    wall_seconds: float = 0.0


def triage(
    run_id: str,
    *,
    store: RunStore,
    scenario: Scenario,
    agent_id: str,
    model: str,
    sandbox: SandboxProtocol,
    config: TriageConfig,
) -> TriageReport:
    started = time.time()
    ledger = BudgetLedger(config.caps)
    findings: list[Finding] = []
    dropped: list[DroppedCandidate] = []

    for record in store.read(run_id, "candidates"):
        outcome = _triage_one(
            record,
            run_id=run_id,
            store=store,
            scenario=scenario,
            agent_id=agent_id,
            model=model,
            sandbox=sandbox,
            config=config,
            ledger=ledger,
        )
        if isinstance(outcome, Finding):
            findings.append(outcome)
            store.append(run_id, "findings", outcome.to_record())
        else:
            dropped.append(outcome)
            store.append(run_id, "dropped", outcome.model_dump(mode="json"))

    reductions = [f.reduction for f in findings]
    return TriageReport(
        run_id=run_id,
        findings=findings,
        groups=dedupe(findings),
        dropped=dropped,
        cost_usd=ledger.spent_usd,
        median_reduction=statistics.median(reductions) if reductions else 0.0,
        wall_seconds=time.time() - started,
    )


def _triage_one(
    record: dict[str, Any],
    *,
    run_id: str,
    store: RunStore,
    scenario: Scenario,
    agent_id: str,
    model: str,
    sandbox: SandboxProtocol,
    config: TriageConfig,
    ledger: BudgetLedger,
) -> Finding | DroppedCandidate:
    candidate = Candidate.rebuild(
        record["payloads"],
        lineage=[Mutation.model_validate(m) for m in record.get("lineage", [])],
        seed_ids=list(record.get("seed_ids", [])),
    )
    targets = list(record.get("action_keys", []))
    if not targets:
        # The loop records a candidate only when a verdict violated, and
        # `Verdict.violated` is `bool(...severity == "violation")`, so a real
        # candidate always has keys. Without them the Confirmer is untargeted,
        # where a reproduction becomes *any* violation and the reported keys are
        # a union across trials that may never have co-occurred.
        raise ConfigError(
            f"candidate {candidate.id} has no action keys; a reproduction would mean "
            "any violation rather than this one, and the finding would name a union "
            "of failures that may never have happened together"
        )
    seed = int(record.get("seed", 0))

    def confirmer_for(scn: Scenario, *, targets: list[str] = targets) -> Confirmer:
        return Confirmer(
            scn,
            agent_id=agent_id,
            model=model,
            sandbox=sandbox,
            store=store,
            run_id=run_id,
            ledger=ledger,
            threshold=config.threshold,
            min_trials=config.min_trials,
            max_trials=config.max_trials,
            target_width=config.target_width,
            target_action_keys=targets,
            agent_profile=config.agent_profile,
            infra_hosts=config.infra_hosts,
        )

    working = confirmer_for(scenario)
    first = working.confirm(candidate, seed=seed)
    if first.estimate.lo < config.threshold:
        return DroppedCandidate(
            candidate_id=candidate.id,
            reason=_why_dropped("as found", first, config.threshold),
            rate=first.estimate,
        )

    shrunk = shrink(
        candidate,
        confirm=lambda c: working.confirm(c, seed=seed),
        threshold=config.threshold,
        max_steps=config.max_shrink_steps,
    )

    env_removed: list[str] = []
    final_scenario = scenario
    if config.shrink_env:
        env = shrink_environment(
            shrunk.shrunk,
            scenario,
            confirm_with=_env_oracle(confirmer_for, seed),
            threshold=config.threshold,
        )
        env_removed = env.removed
        final_scenario = env.scenario

    # Fresh Confirmer, narrowed scenario, different seed: see the module
    # docstring. This is the only measurement the finding quotes.
    final = confirmer_for(final_scenario).confirm(shrunk.shrunk, seed=seed + 1)
    if final.estimate.lo < config.threshold:
        return DroppedCandidate(
            candidate_id=candidate.id,
            reason=_why_dropped("shrunk form", final, config.threshold),
            rate=final.estimate,
        )

    return Finding(
        scenario_name=final_scenario.name,
        scenario_hash=final_scenario.scenario_hash,
        agent_meta=_agent_meta(store, run_id, final, agent_id, model),
        sandbox_description=sandbox.describe(),
        payloads=shrunk.shrunk.payloads,
        lineage=shrunk.shrunk.lineage,
        seed_ids=shrunk.shrunk.seed_ids,
        action_keys=targets,
        coverage_signature=str(record.get("coverage_signature", "")),
        rate=final.estimate,
        threshold=config.threshold,
        original_bytes=shrunk.original_bytes,
        shrunk_bytes=shrunk.shrunk_bytes,
        env_removed=env_removed,
        # Only the final measurement's trials: they are the evidence for the
        # rate this finding reports, and mixing in the shrink's would make the
        # count disagree with `rate.trials`.
        trial_ids=final.trial_ids,
        cost_usd=first.cost_usd + shrunk.cost_usd + final.cost_usd,
    )


def _why_dropped(stage: str, got: Confirmation, threshold: float) -> str:
    """Never report a negative result from a measurement that never happened.

    `estimate(0, 0)` is the whole of [0, 1], and `stats` deliberately makes it
    decisive in neither direction -- so a confirmation that ran no usable trial
    is not evidence that the finding failed to reproduce. Saying "did not hold
    up: 0% (0/0)" would be the handoff's recurring bug in report form: a
    component that measured nothing announcing a clean result. The usual cause
    is a budget cap tripping mid-confirmation, which the Confirmation's own
    `stop_reason` names.
    """
    if got.estimate.trials == 0:
        return f"{stage}: never measured ({got.stop_reason or 'no usable trial'})"
    return (
        f"{stage}: rate lower bound {got.estimate.lo:.2f} below threshold "
        f"{threshold:.2f} ({got.estimate.summary()})"
    )


def _env_oracle(
    confirmer_for: Callable[[Scenario], Confirmer], seed: int
) -> Callable[[Scenario, Candidate], Confirmation]:
    """One Confirmer per narrowed scenario, because a Confirmer is bound to one.

    Its cache keys on the scenario hash anyway, so a shared one could not serve
    two scenarios, and `shrink_environment` never asks the same question twice.
    """

    def confirm_with(scn: Scenario, cand: Candidate) -> Confirmation:
        return confirmer_for(scn).confirm(cand, seed=seed)

    return confirm_with


def _agent_meta(
    store: RunStore, run_id: str, final: Confirmation, agent_id: str, model: str
) -> AgentMeta:
    """What actually ran, taken from the trials the finding's rate comes from.

    The draft looked up the search's `first_trial_id` instead, falling back to
    `agent_version="unknown"`. The measurement the finding quotes is the better
    source, for the same reason its scenario hash is: the agent version and
    container digest should describe the run the reported rate came from, not an
    earlier one that may have used a different image. An exported test cannot
    pin to "unknown", so filling that in silently is the worst option available.

    The lookup cannot miss, and the fallback is gone rather than left untested:
    a surviving finding has `estimate.trials > 0` (the drop above guarantees it),
    so `final.trial_ids` is non-empty, and `run_trial` appends every trial to the
    store before returning. A `KeyError` here would mean the store lost a line
    this process wrote moments ago, which is worth hearing about loudly.
    """
    by_id = {t.get("trial_id"): t for t in store.read(run_id, "trials")}
    trial = by_id[final.trial_ids[0]]
    return AgentMeta(
        agent_id=str(trial.get("agent_id") or agent_id),
        agent_version=str(trial.get("agent_version") or "unknown"),
        model_id=str(trial.get("model_id") or model),
        prompt_hash=str(trial.get("prompt_hash") or ""),
        container_digest=str(trial.get("container_digest") or ""),
    )


__all__ = ["DroppedCandidate", "TriageConfig", "TriageReport", "triage"]
