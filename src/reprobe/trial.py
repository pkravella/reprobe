"""One trial, end to end: mint canaries, run the sandbox, check, record.

This is the function the search loop calls thousands of times and the exported
regression test calls once. Keeping both on the same path is what makes an
exported test mean the same thing as the finding it came from.
"""

from __future__ import annotations

import random
import time
from typing import Any

from pydantic import BaseModel, Field

from reprobe.agents.base import AgentSpec
from reprobe.agents.fake_agent import PROFILES, FakeAgentAdapter
from reprobe.budget import BudgetLedger
from reprobe.canary import mint
from reprobe.checks import Verdict, evaluate
from reprobe.ids import new_id
from reprobe.sandbox import SandboxProtocol, TrialResult, TrialSpec
from reprobe.scenario import Scenario
from reprobe.store import RunStore
from reprobe.trace import export_otel


class TrialRecord(BaseModel):
    model_config = {"frozen": True}

    trial_id: str
    verdict: Verdict | None
    result: TrialResult
    seed: int
    payload_refs: dict[str, str] = Field(default_factory=dict)
    trace_ref: str = ""
    duration_s: float = 0.0

    @property
    def harness_error(self) -> str | None:
        return self.result.harness_error

    @property
    def violated(self) -> bool:
        return bool(self.verdict and self.verdict.violated)


def run_trial(
    scenario: Scenario,
    *,
    agent_id: str,
    model: str,
    payloads: dict[str, str],
    sandbox: SandboxProtocol,
    store: RunStore,
    run_id: str,
    seed: int,
    ledger: BudgetLedger,
    otel_endpoint: str | None = None,
    extra_args: list[str] | None = None,
    agent_profile: str | None = None,
    infra_hosts: tuple[str, ...] = (),
) -> TrialRecord:
    # Before any spend, by design (R12): the ledger decides whether this trial
    # may run at all, and a tripped cap raises BudgetExceeded here.
    ledger.check_can_dispatch()

    rng = random.Random(seed)
    env_overrides: dict[str, str] = {}
    if agent_profile is not None:
        # The fake agent reads its behaviour from the environment. The seed is
        # the PER-TRIAL seed on purpose: with the run seed, every trial of a
        # flaky profile would give the same answer and the rate estimator would
        # never be exercised.
        env_overrides.update(FakeAgentAdapter.profile_env(agent_profile, seed))

    spec = TrialSpec(
        trial_id=new_id("trial"),
        scenario=scenario,
        agent=AgentSpec(id=agent_id, model=model, extra_args=extra_args or []),
        payloads=payloads,
        canaries=[mint(canary, rng) for canary in scenario.canaries],
        seed=seed,
        env_overrides=env_overrides,
    )

    started = time.time()
    result = sandbox.run(spec)
    ledger.record(result.cost)

    # A harness error means no verdict, not a clean pass. `evaluate` would raise
    # on such a result, so it is never called.
    verdict = (
        None
        if not result.ok
        else evaluate(scenario, result, spec.canaries, infra_hosts=infra_hosts)
    )

    trace_ref = store.put_blob(result.trace.to_jsonl().encode("utf-8"))
    payload_refs = {sid: store.put_blob(text.encode("utf-8")) for sid, text in payloads.items()}
    if otel_endpoint:
        export_otel(result.trace, endpoint=otel_endpoint)

    record = TrialRecord(
        trial_id=spec.trial_id,
        verdict=verdict,
        result=result,
        seed=seed,
        payload_refs=payload_refs,
        trace_ref=trace_ref,
        duration_s=time.time() - started,
    )
    store.append(run_id, "trials", _store_record(record, scenario, spec))
    return record


def _store_record(record: TrialRecord, scenario: Scenario, spec: TrialSpec) -> dict[str, Any]:
    """Everything needed to replay this trial. PRD: "any finding can be replayed"."""
    meta = record.result.agent_meta
    return {
        "trial_id": record.trial_id,
        "scenario_name": scenario.name,
        "scenario_hash": scenario.scenario_hash,
        "agent_id": meta.agent_id,
        "agent_version": meta.agent_version,
        "model_id": meta.model_id,
        "prompt_hash": meta.prompt_hash,
        "container_digest": meta.container_digest,
        "seed": record.seed,
        "payload_refs": record.payload_refs,
        "trace_ref": record.trace_ref,
        "verdict": record.verdict.model_dump() if record.verdict else None,
        "harness_error": record.result.harness_error,
        "exit_code": record.result.exit_code,
        "cost_usd": record.result.cost.usd,
        "input_tokens": record.result.cost.input_tokens,
        "output_tokens": record.result.cost.output_tokens,
        "duration_s": record.duration_s,
        "canary_ids": [canary.id for canary in spec.canaries],
    }


def known_profiles() -> list[str]:
    """Named fake-agent profiles, for the CLI to validate `--agent-profile`."""
    return sorted(PROFILES)
