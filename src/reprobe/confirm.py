"""R9: how often does this payload actually fail?

Four decisions that matter more than the code:

1. **Harness failures leave the denominator.** A trial the harness broke tells
   us nothing about the agent, so counting it as a non-reproduction would bias
   every rate downward and make exported tests look flakier than the agent is.
   They are counted and reported separately, and a candidate whose every trial
   broke stops on that rather than on a rate -- with no usable trial there is no
   estimate for a stopping rule to read.

2. **A reproduction is the same violation.** Without `target_action_keys`, a
   shrinker would happily reduce a canary-exfiltration payload into a
   dangerous-command payload and report a great reproduction rate for the wrong
   finding. Untargeted, a reproduction is *any* violation and the reported keys
   are the union over trials, which can name two failures that never occurred
   in the same trial -- fine for the first look at a fresh candidate, which is
   the only thing that uses it, and the reason the shrinker never does.

3. **A measurement already paid for is never thrown away.** Both the budget
   check here and the one inside `run_trial` can trip, and an exception out of
   `confirm` would discard every trial banked so far. Either path ends the loop
   and returns what was measured.

4. **The cache ignores the seed.** The shrinker re-asks about the same reduced
   payload constantly and passes a fresh seed each time; keying on the seed
   would make every lookup a miss and turn the cache into pure overhead. The
   key is the payload content, the scenario, the agent, the model and the
   targets -- everything that changes what is being measured.
"""

from __future__ import annotations

import random
from typing import Any

from pydantic import BaseModel, Field

from reprobe.budget import BudgetLedger
from reprobe.errors import BudgetExceeded
from reprobe.ids import digest
from reprobe.mutate import Candidate
from reprobe.sandbox import SandboxProtocol
from reprobe.scenario import Scenario
from reprobe.stats import DEFAULT_THRESHOLD, RateEstimate, estimate, should_stop
from reprobe.store import RunStore
from reprobe.trial import run_trial


class Confirmation(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    candidate: Candidate
    estimate: RateEstimate
    action_keys: list[str] = Field(default_factory=list)
    trial_ids: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    harness_failures: int = 0
    stop_reason: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate.id,
            "payloads": self.candidate.payloads,
            "successes": self.estimate.successes,
            "trials": self.estimate.trials,
            "rate_point": self.estimate.point,
            "rate_lo": self.estimate.lo,
            "rate_hi": self.estimate.hi,
            "action_keys": self.action_keys,
            "trial_ids": self.trial_ids,
            "cost_usd": self.cost_usd,
            "harness_failures": self.harness_failures,
            "stop_reason": self.stop_reason,
        }


class Confirmer:
    def __init__(
        self,
        scenario: Scenario,
        *,
        agent_id: str,
        model: str,
        sandbox: SandboxProtocol,
        store: RunStore,
        run_id: str,
        ledger: BudgetLedger,
        threshold: float = DEFAULT_THRESHOLD,
        min_trials: int = 5,
        max_trials: int = 40,
        target_width: float = 0.25,
        target_action_keys: list[str] | None = None,
        agent_profile: str | None = None,
        infra_hosts: tuple[str, ...] = (),
    ) -> None:
        self._scenario = scenario
        self._agent_id = agent_id
        self._model = model
        self._sandbox = sandbox
        self._store = store
        self._run_id = run_id
        self._ledger = ledger
        self._threshold = threshold
        self._min_trials = min_trials
        self._max_trials = max_trials
        self._target_width = target_width
        self._targets = set(target_action_keys or [])
        self._agent_profile = agent_profile
        self._infra_hosts = infra_hosts
        self._cache: dict[str, Confirmation] = {}
        self.calls = 0
        self.cache_hits = 0

    def _cache_key(self, candidate: Candidate) -> str:
        # `candidate.id` is already the content hash of the payloads, so the
        # payloads themselves do not need rehashing here.
        return digest(
            {
                "candidate": candidate.id,
                "scenario": self._scenario.scenario_hash,
                "agent": self._agent_id,
                "model": self._model,
                "targets": sorted(self._targets),
            }
        )

    def confirm(self, candidate: Candidate, *, seed: int) -> Confirmation:
        # Ahead of the cache: a candidate aimed at a surface this scenario does
        # not declare is a bug to report, not a result to remember.
        self._scenario.check_payloads(candidate.payloads)

        key = self._cache_key(candidate)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]

        self.calls += 1
        rng = random.Random(seed)
        successes = 0
        usable = 0
        harness_failures = 0
        cost = 0.0
        trial_ids: list[str] = []
        observed: set[str] = set()
        reason = ""

        while True:
            try:
                self._ledger.check_can_dispatch()
                record = run_trial(
                    self._scenario,
                    agent_id=self._agent_id,
                    model=self._model,
                    payloads=candidate.payloads,
                    sandbox=self._sandbox,
                    store=self._store,
                    run_id=self._run_id,
                    seed=rng.randrange(2**31),
                    ledger=self._ledger,
                    agent_profile=self._agent_profile,
                    infra_hosts=self._infra_hosts,
                )
            except BudgetExceeded as exc:
                # Both the check above and `run_trial`'s own check raise this.
                # Either way the trials already banked are the measurement.
                reason = f"budget exhausted: {exc}"
                break

            cost += record.result.cost.usd
            trial_ids.append(record.trial_id)

            if record.harness_error:
                harness_failures += 1
                # No usable trial means no estimate, so no rule below can ever
                # fire. Without this the loop would not terminate.
                if harness_failures > self._max_trials:
                    reason = f"too many harness failures ({harness_failures})"
                    break
                continue

            usable += 1
            keys = set(record.verdict.action_keys) if record.verdict else set()
            observed |= keys
            if self._counts_as_reproduction(keys):
                successes += 1

            decision = should_stop(
                estimate(successes, usable),
                threshold=self._threshold,
                min_trials=self._min_trials,
                max_trials=self._max_trials,
                target_width=self._target_width,
            )
            if decision.stop:
                reason = decision.reason
                break

        final = Confirmation(
            candidate=candidate,
            estimate=estimate(successes, usable),
            action_keys=sorted(self._targets or observed),
            trial_ids=trial_ids,
            cost_usd=cost,
            harness_failures=harness_failures,
            stop_reason=reason,
        )
        self._cache[key] = final
        self._store.append(self._run_id, "confirmations", final.to_record())
        return final

    def _counts_as_reproduction(self, keys: set[str]) -> bool:
        if not keys:
            return False
        if not self._targets:
            return True
        return bool(keys & self._targets)


__all__ = ["Confirmation", "Confirmer"]
