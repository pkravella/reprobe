"""The search loop: the top two rows of the PRD's architecture diagram.

    pick candidate -> run trial -> fingerprint trace -> update coverage
         ^                                                     |
         +-----------------------------------------------------+

Concurrency is a thread pool, not asyncio: every trial is a blocking
subprocess-and-container operation, the pool is small (bounded by
`caps.max_concurrency`), and the shared state is one scheduler guarded by a
lock. Threads keep the code readable and the stack traces honest.

Two things are deliberate and easy to get wrong:

**The whole configuration is validated before the first trial is dispatched.**
A misspelled coverage group or an unknown agent profile is cheap to catch and
expensive to discover on trial one of a thousand.

**The loop counts its own dispatches against `max_trials`.** The ledger counts a
trial when it records a cost, which happens after the trial returns, so
dispatching against the ledger alone overshoots by up to `concurrency - 1`
trials. R12 says the caps are enforced, not advisory.
"""

from __future__ import annotations

import dataclasses
import random
import threading
import time
from concurrent.futures import ALL_COMPLETED, FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from reprobe.agents import get_adapter
from reprobe.budget import BudgetCaps, BudgetLedger
from reprobe.coverage import SIGNAL_GROUPS, check_groups, fingerprint
from reprobe.errors import BudgetExceeded, ConfigError
from reprobe.mutate import Candidate, MutationContext
from reprobe.sandbox import SandboxProtocol
from reprobe.scenario import Scenario
from reprobe.schedule import EnergyScheduler, RandomScheduler, Scheduler
from reprobe.seeds import load_seeds
from reprobe.store import RunStore
from reprobe.trial import TrialRecord, known_profiles, run_trial

SCHEDULERS = ("energy", "random")


class FuzzConfig(BaseModel):
    model_config = {"frozen": True}

    scenario: Scenario
    agent_id: str
    model: str
    scheduler: Literal["energy", "random"] = "energy"
    seed: int = 0
    caps: BudgetCaps
    coverage_groups: tuple[str, ...] = SIGNAL_GROUPS
    #: `.invalid` is reserved by RFC 2606 and can never resolve, which is the
    #: same rule the seed corpus is held to.
    collector_host: str = "http://collector.invalid"
    seed_paths: tuple[Path, ...] = ()
    agent_profile: str | None = None
    otel_endpoint: str | None = None
    #: Recorded in the run metadata so `reprobe triage` can reload the same
    #: scenario rather than being told again. A `Scenario` does not carry the
    #: path it was loaded from, and guessing it from `fixture_dir` would pick a
    #: file that may not be the one the search ran against.
    scenario_path: Path | None = None
    #: Baseline arm only: lets the gate run a depth-matched control, so a win
    #: cannot be a mutation-count advantage reported as a guidance one.
    baseline_mutations: int = 1


class CandidateFinding(BaseModel):
    model_config = {"frozen": True}

    candidate: Candidate
    action_keys: list[str]
    coverage_signature: str
    first_trial_id: str
    seed: int

    def to_record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate.id,
            "payloads": self.candidate.payloads,
            "lineage": [m.model_dump() for m in self.candidate.lineage],
            "seed_ids": self.candidate.seed_ids,
            "action_keys": self.action_keys,
            "coverage_signature": self.coverage_signature,
            "first_trial_id": self.first_trial_id,
            "seed": self.seed,
        }


class FuzzResult(BaseModel):
    model_config = {"frozen": True}

    run_id: str
    trials: int
    violations: list[CandidateFinding] = Field(default_factory=list)
    #: Trials that produced a violation, including repeats of one payload.
    #:
    #: Distinct from `len(violations)`, and both are needed. A guided search
    #: revisits a payload that works, so counting unique payloads understates
    #: it; a blind search scatters, so counting trials overstates how much it
    #: actually found. Collapsing payloads that reach the *same violating
    #: action* into one finding is R16's job, in triage.
    violating_trials: int = 0
    harness_failures: int = 0
    cost_usd: float = 0.0
    scheduler_stats: dict[str, Any] = Field(default_factory=dict)
    wall_seconds: float = 0.0


def _validate(config: FuzzConfig) -> None:
    """Everything that can be known to be wrong before anything is spent."""
    # The scheduler name needs no check here: the `Literal` on `FuzzConfig`
    # rejects an unknown one at construction, and the CLI turns that into a
    # sentence. A second check would be unreachable.
    check_groups(config.coverage_groups)
    if config.agent_profile is not None and config.agent_profile not in known_profiles():
        raise ConfigError(
            f"unknown agent profile {config.agent_profile!r}; known: {known_profiles()}"
        )
    if not config.scenario.surfaces:
        raise ConfigError(
            f"scenario {config.scenario.name!r} declares no attacker-controlled surface, "
            "so there is nothing to search"
        )


def _build_scheduler(config: FuzzConfig, ctx: MutationContext) -> Scheduler:
    seeds = load_seeds(list(config.seed_paths))
    if config.scheduler == "random":
        return RandomScheduler(seeds, ctx, mutations_per_candidate=config.baseline_mutations)
    return EnergyScheduler(seeds, ctx)


def fuzz(config: FuzzConfig, *, sandbox: SandboxProtocol, store: RunStore) -> FuzzResult:
    _validate(config)
    scenario = config.scenario
    ctx = MutationContext(
        scenario=scenario,
        canary_paths={c.id: c.path for c in scenario.canaries if c.path},
        collector=config.collector_host,
        surfaces=[s.id for s in scenario.surfaces],
    )
    scheduler = _build_scheduler(config, ctx)
    ledger = BudgetLedger(config.caps)
    infra_hosts = get_adapter(config.agent_id).infra_hosts
    run_id = store.open_run(
        {
            "command": "fuzz",
            "scenario": scenario.name,
            "scenario_path": str(config.scenario_path) if config.scenario_path else None,
            "scenario_hash": scenario.scenario_hash,
            "agent": config.agent_id,
            "model": config.model,
            "agent_profile": config.agent_profile,
            "scheduler": config.scheduler,
            "seed": config.seed,
            "coverage_groups": list(config.coverage_groups),
            "caps": dataclasses.asdict(config.caps),
            "sandbox": sandbox.describe(),
        }
    )

    lock = threading.Lock()
    rng = random.Random(config.seed)
    started = time.time()
    state = _LoopState()

    def one_trial(candidate: Candidate, trial_seed: int) -> TrialRecord:
        return run_trial(
            scenario,
            agent_id=config.agent_id,
            model=config.model,
            payloads=candidate.payloads,
            sandbox=sandbox,
            store=store,
            run_id=run_id,
            seed=trial_seed,
            ledger=ledger,
            otel_endpoint=config.otel_endpoint,
            agent_profile=config.agent_profile,
            infra_hosts=infra_hosts,
        )

    def absorb(candidate: Candidate, record: TrialRecord) -> None:
        if record.harness_error:
            # No verdict and no trace worth believing. Fingerprinting it would
            # teach the scheduler that crashing is new behaviour worth chasing.
            state.harness_failures += 1
            return
        cov = fingerprint(record.result.trace, groups=config.coverage_groups)
        novelty = scheduler.coverage_map.update(cov)
        scheduler.observe(candidate, cov, novelty, violated=record.violated)
        if not record.violated:
            return
        state.violating_trials += 1
        if candidate.id not in state.found:
            finding = CandidateFinding(
                candidate=candidate,
                action_keys=record.verdict.action_keys if record.verdict else [],
                coverage_signature=cov.signature(),
                first_trial_id=record.trial_id,
                seed=record.seed,
            )
            state.found[candidate.id] = finding
            store.append(run_id, "candidates", finding.to_record())

    workers = max(1, config.caps.max_concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: dict[Future[TrialRecord], Candidate] = {}
        while state.dispatched < config.caps.max_trials:
            try:
                ledger.check_can_dispatch()
            except BudgetExceeded:
                break
            with lock:
                candidate = scheduler.next_candidate(rng)
                trial_seed = rng.randrange(2**31)
            pending[pool.submit(one_trial, candidate, trial_seed)] = candidate
            state.dispatched += 1
            if len(pending) >= workers:
                _drain(pending, absorb, lock)
        while pending:
            _drain(pending, absorb, lock, all_of_them=True)

    return FuzzResult(
        run_id=run_id,
        trials=state.dispatched,
        violations=list(state.found.values()),
        violating_trials=state.violating_trials,
        harness_failures=state.harness_failures,
        cost_usd=ledger.spent_usd,
        scheduler_stats=scheduler.stats(),
        wall_seconds=time.time() - started,
    )


class _LoopState:
    """Counters the worker callbacks mutate. A class, not `nonlocal`, so the
    callbacks stay readable and mypy can see the types."""

    def __init__(self) -> None:
        self.dispatched = 0
        self.violating_trials = 0
        self.harness_failures = 0
        self.found: dict[str, CandidateFinding] = {}


def _drain(
    pending: dict[Future[TrialRecord], Candidate],
    absorb: Any,
    lock: threading.Lock,
    *,
    all_of_them: bool = False,
) -> None:
    done, _ = wait(list(pending), return_when=ALL_COMPLETED if all_of_them else FIRST_COMPLETED)
    for future in done:
        candidate = pending.pop(future)
        try:
            record = future.result()
        except BudgetExceeded:
            # A trial that lost the race for the last of the budget. Not a
            # harness failure and not a result; it simply never ran.
            continue
        with lock:
            absorb(candidate, record)
