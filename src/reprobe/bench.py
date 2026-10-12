"""The benchmark behind the PRD's headline claim: guided search against a
random-mutation baseline, on equal trial budgets.

For each scenario x seed x scheduler it runs `fuzz`, groups the candidates by
violating action (R16's unit), and confirms one representative per group. A
group counts as a **unique reproducible finding** only if its own violation
reproduces with a Wilson lower bound at or above the threshold. Shrinking is
skipped: it does not change whether a finding is reproducible, and the
reduction it buys is measured by the Phase-3 gate, not here.

Honesty rules, each with a test:

* **The unit follows the money.** The PRD's metric is findings per $100. On
  the free lanes nothing is spent, and per $100 is a division by zero -- not a
  big number, a metric that does not apply -- so it becomes findings per 1,000
  trials, and says so.
* **Seeds are the unit of independence.** Every arm is reported per seed, and
  nothing is aggregated without the spread beside it: a 2x ratio from one lucky
  seed is not a result. (Scenarios are *not* independent under a
  placement-independent trigger -- see docs/benchmark-v0.1.md.)
* **A ratio with nothing under it is not reported.** If the baseline found
  nothing, the ratio is n/a, and the strict "found what the baseline missed"
  count says what happened instead.
* **Time to first violation is counted in trials, in dispatch order, and
  censored.** A seed that never violates is "> budget", not a number; once
  most are, the median is a bound. Wall-clock time is constant per trial on the scripted agent and
  confounded by concurrency, so it would only restate the trial count.
* **The runtime and the agent are printed**, so a simulated run can never be
  read as a claim about a real agent.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from reprobe.agents import get_adapter
from reprobe.agents.fake_agent import FakeAgentAdapter
from reprobe.budget import BudgetCaps, BudgetLedger
from reprobe.confirm import Confirmer
from reprobe.errors import ConfigError
from reprobe.loop import FuzzConfig, fuzz
from reprobe.mutate import Candidate
from reprobe.sandbox import SandboxProtocol
from reprobe.scenario import load_scenario
from reprobe.stats import DEFAULT_THRESHOLD
from reprobe.store import RunStore

Scheduler = Literal["energy", "random"]


@dataclass(frozen=True)
class BenchConfig:
    scenarios: Sequence[Path]
    agent_id: str
    model: str
    trials_per_arm: int
    seeds: Sequence[int]
    #: Across the whole benchmark. 0 means it must cost nothing, as everywhere.
    max_usd: float
    threshold: float = DEFAULT_THRESHOLD
    agent_profile: str | None = None
    concurrency: int = 4
    schedulers: tuple[Scheduler, ...] = ("energy", "random")
    #: Confirmation budget per group: decides reproducibility, nothing more.
    confirm_max_trials: int = 20


@dataclass(frozen=True)
class ArmResult:
    scheduler: str
    scenario: str
    seed: int
    trials: int
    violating_trials: int
    candidates: int
    #: Distinct violating-action sets among the candidates.
    groups: int
    #: Groups whose own violation reproduced above the threshold.
    unique_findings: int
    finding_keys: list[str]
    cost_usd: float
    #: 1-based position of the first violating trial; None if none violated.
    trials_to_first_violation: int | None
    runtime: str
    #: Groups the dollar cap stopped from being confirmed: not counted either way.
    unconfirmed: int = 0


@dataclass(frozen=True)
class BenchReport:
    config: BenchConfig
    arms: list[ArmResult] = field(default_factory=list)
    runtime: str = "unknown"
    budget_exhausted: bool = False
    #: (scenario file, seed, scheduler) of every arm the cap stopped from running.
    skipped: list[tuple[str, int, str]] = field(default_factory=list)


def _check(config: BenchConfig) -> None:
    empty = [
        name
        for name, value in (
            ("scenarios", config.scenarios),
            ("seeds", config.seeds),
            ("schedulers", config.schedulers),
        )
        if not value
    ]
    if empty or config.trials_per_arm < 1:
        raise ConfigError(f"nothing to benchmark: {', '.join(empty) or 'trials_per_arm'} is empty")


def _arm(
    config: BenchConfig,
    scenario_path: Path,
    scheduler: Scheduler,
    seed: int,
    *,
    sandbox: SandboxProtocol,
    store: RunStore,
    budget_left: float,
) -> ArmResult:
    scenario = load_scenario(scenario_path)
    adapter = get_adapter(config.agent_id)
    result = fuzz(
        FuzzConfig(
            scenario=scenario,
            scenario_path=scenario_path.resolve(),
            agent_id=config.agent_id,
            model=config.model,
            scheduler=scheduler,
            seed=seed,
            caps=BudgetCaps(
                max_usd=budget_left,
                max_trials=config.trials_per_arm,
                max_concurrency=config.concurrency,
            ),
            agent_profile=config.agent_profile,
        ),
        sandbox=sandbox,
        store=store,
    )
    groups: dict[tuple[str, ...], Candidate] = {}
    for found in result.violations:
        groups.setdefault(tuple(sorted(found.action_keys)), found.candidate)

    # With a cap, confirmation gets what the search left. Never a ledger of 0
    # when the cap is spent: 0 means "free only" and lets the first paid trial
    # through, so a $5 cap spent $6. A spent cap skips confirmation instead,
    # and the groups are reported as unconfirmed, not as not reproducible.
    remaining = budget_left - result.cost_usd if budget_left > 0 else 0.0
    exhausted = budget_left > 0 and remaining <= 0
    ledger = BudgetLedger(
        BudgetCaps(max_usd=max(remaining, 0.0), max_trials=1_000_000, max_concurrency=1)
    )
    rng = random.Random(seed)
    confirmed: list[str] = []
    unconfirmed = 0
    for keys, candidate in groups.items():
        if exhausted or (budget_left == 0 and result.cost_usd > 0):
            unconfirmed += 1
            continue
        confirmation = Confirmer(
            scenario,
            agent_id=config.agent_id,
            model=config.model,
            sandbox=sandbox,
            store=store,
            run_id=result.run_id,
            ledger=ledger,
            threshold=config.threshold,
            min_trials=5,
            max_trials=config.confirm_max_trials,
            target_width=0.0,
            target_action_keys=list(keys),
            agent_profile=config.agent_profile,
            infra_hosts=adapter.infra_hosts,
        ).confirm(candidate, seed=rng.randrange(2**31))
        if confirmation.estimate.lo >= config.threshold:
            confirmed.append(",".join(keys))
        elif confirmation.stop_reason.startswith("budget"):
            unconfirmed += 1

    return ArmResult(
        scheduler=scheduler,
        scenario=scenario.name,
        seed=seed,
        trials=result.trials,
        violating_trials=result.violating_trials,
        candidates=len(result.violations),
        groups=len(groups),
        unique_findings=len(confirmed),
        finding_keys=sorted(confirmed),
        cost_usd=result.cost_usd + ledger.spent_usd,
        trials_to_first_violation=result.first_violation,
        unconfirmed=unconfirmed,
        runtime=str(sandbox.describe().get("runtime", "unknown")),
    )


def benchmark(
    config: BenchConfig,
    *,
    sandbox_factory: Callable[[], SandboxProtocol],
    store_root: Path,
) -> BenchReport:
    _check(config)
    store = RunStore(Path(store_root))
    arms: list[ArmResult] = []
    skipped: list[tuple[str, int, str]] = []
    spent = 0.0
    exhausted = False
    for scenario_path in config.scenarios:
        for seed in config.seeds:
            for scheduler in config.schedulers:
                # A cap of 0 means it must cost nothing: one arm that spent
                # anything stops the rest, rather than each spending a little.
                if (config.max_usd > 0 and spent >= config.max_usd) or (
                    config.max_usd == 0 and spent > 0
                ):
                    exhausted = True
                    skipped.append((Path(scenario_path).parent.name, seed, scheduler))
                    continue
                left = config.max_usd - spent if config.max_usd > 0 else 0.0
                arm = _arm(
                    config,
                    Path(scenario_path),
                    scheduler,
                    seed,
                    sandbox=sandbox_factory(),
                    store=store,
                    budget_left=left,
                )
                spent += arm.cost_usd
                arms.append(arm)
    runtimes = {a.runtime for a in arms}
    return BenchReport(
        config=config,
        arms=arms,
        runtime=", ".join(sorted(runtimes)) or "unknown",
        budget_exhausted=(
            exhausted
            or (config.max_usd > 0 and spent >= config.max_usd)
            or (config.max_usd == 0 and spent > 0)
        ),
        skipped=skipped,
    )


# --- metrics ------------------------------------------------------------------


def _unit(arms: Sequence[ArmResult]) -> str:
    return "per $100" if sum(a.cost_usd for a in arms) > 0 else "per 1,000 trials"


def efficiency(arms: Sequence[ArmResult], unit: str | None = None) -> tuple[float, str]:
    """Unique reproducible findings per $100, or per 1,000 trials when free."""
    unit = unit or _unit(arms)
    found = sum(a.unique_findings for a in arms)
    if unit == "per $100":
        cost = sum(a.cost_usd for a in arms)
        return (found / cost * 100 if cost else 0.0), unit
    trials = sum(a.trials for a in arms)
    return (found / trials * 1000 if trials else 0.0), unit


def efficiency_ratio(arms: Sequence[ArmResult]) -> float | None:
    """Guided over random, in one unit; None when the baseline found nothing."""
    unit = _unit(arms)
    guided = [a for a in arms if a.scheduler == "energy"]
    baseline = [a for a in arms if a.scheduler == "random"]
    base, _ = efficiency(baseline, unit)
    if not guided or not base:
        return None
    return efficiency(guided, unit)[0] / base


def censored_median(values: Sequence[int | None], *, budget: int) -> str:
    """The median of right-censored counts: a number, or a bound.

    None is "did not happen within `budget`", which sorts above every number.
    The lower middle is taken for an even count, so the claim errs small.
    """
    ordered = sorted(values, key=lambda v: (v is None, v or 0))
    middle = ordered[(len(ordered) - 1) // 2]
    return f"> {budget}" if middle is None else str(middle)


def _missed_by_baseline(arms: Sequence[ArmResult]) -> tuple[int, int]:
    """(scenario, seed) pairs where guided confirmed a group random did not."""
    # Only where both arms ran: a baseline the cap stopped is not a baseline
    # that found nothing.
    ran = {(a.scenario, a.seed, a.scheduler) for a in arms}
    pairs = {
        (scenario, seed)
        for scenario, seed, _ in ran
        if (scenario, seed, "energy") in ran and (scenario, seed, "random") in ran
    }
    wins = 0
    for scenario, seed in pairs:
        guided = {
            k
            for a in arms
            if (a.scenario, a.seed, a.scheduler) == (scenario, seed, "energy")
            for k in a.finding_keys
        }
        base = {
            k
            for a in arms
            if (a.scenario, a.seed, a.scheduler) == (scenario, seed, "random")
            for k in a.finding_keys
        }
        wins += bool(guided - base)
    return wins, len(pairs)


# --- the write-up -----------------------------------------------------------------


def render_bench_markdown(report: BenchReport) -> str:
    cfg = report.config
    arms = report.arms
    unit = _unit(arms)
    lines = [
        "# Reprobe benchmark: guided vs random",
        "",
        f"runtime `{report.runtime}` · agent `{cfg.agent_id}` · model `{cfg.model}`"
        + (f" · profile `{cfg.agent_profile}`" if cfg.agent_profile else "")
        + f" · {len(cfg.scenarios)} scenario(s) · seeds {', '.join(map(str, cfg.seeds))}"
        + f" · {cfg.trials_per_arm} trials per arm · concurrency {cfg.concurrency}"
        + f" · threshold {cfg.threshold:.0%}",
        "",
    ]
    if cfg.agent_id == FakeAgentAdapter.id:
        lines += [
            f"**The agent is scripted** (the fake agent, profile `{cfg.agent_profile}`). This "
            "measures the search against a susceptibility designed in advance, and says "
            "nothing about how a real agent would respond to the same payloads.",
            "",
        ]
    lines += ["## Headline", ""]
    ran = {a.scheduler for a in arms}
    for scheduler in cfg.schedulers:
        if scheduler not in ran:
            # Not 0.0: an arm that never ran has no result, and a zero would
            # read as one.
            lines.append(f"- {scheduler}: did not run (the dollar cap was spent first)")
            continue
        value, _ = efficiency([a for a in arms if a.scheduler == scheduler], unit)
        lines.append(f"- {scheduler}: {value:.1f} unique reproducible findings {unit}")
    ratio = efficiency_ratio(arms)
    lines.append(
        f"- ratio: {ratio:.2f}x guided over random"
        if ratio is not None
        else "- ratio: n/a (the baseline confirmed nothing, so there is nothing to divide by)"
    )
    wins, pairs = _missed_by_baseline(arms)
    lines.append(
        f"- guided confirmed a finding the baseline did not on {wins} of {pairs} "
        "scenario-seed pair(s)"
    )
    for scheduler in [s for s in cfg.schedulers if s in ran]:
        firsts = [a.trials_to_first_violation for a in arms if a.scheduler == scheduler]
        lines.append(
            f"- median trials to first violation, {scheduler}: "
            f"{censored_median(firsts, budget=cfg.trials_per_arm)} "
            f"({sum(f is None for f in firsts)} of {len(firsts)} never violated)"
        )
    lines.append(f"- cost: ${sum(a.cost_usd for a in arms):.2f}")
    if report.budget_exhausted:
        if cfg.max_usd == 0:
            lines.append(
                "- **--max-usd 0 means it must cost nothing, and a trial cost money; the "
                "remaining arms did not run**"
            )
        else:
            lines.append("- **the dollar cap was reached; some arms ran short or not at all**")
    if report.skipped:
        lines.append(
            "- did not run: "
            + ", ".join(
                f"{scheduler} seed {seed} ({scn})" for scn, seed, scheduler in report.skipped
            )
        )
    unconfirmed = sum(a.unconfirmed for a in arms)
    if unconfirmed:
        lines.append(
            f"- {unconfirmed} group(s) could not be confirmed before the dollar cap; they are "
            "counted neither as reproducible nor as not"
        )
    lines += [
        "",
        "A unique reproducible finding is a group of candidates with the same violating "
        "actions, one of which reproduced that violation with a Wilson lower bound at or "
        f"above {cfg.threshold:.0%} on re-measurement. Arms are listed per seed because "
        "seeds, not scenarios, are the unit of independence.",
        "",
        "## Per seed",
        "",
        "| Scenario | Seed | Scheduler | Trials | Violating trials | Candidates | Groups "
        "| Reproducible | First violation | Cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for a in arms:
        first = a.trials_to_first_violation
        lines.append(
            f"| {a.scenario} | {a.seed} | {a.scheduler} | {a.trials} | {a.violating_trials} "
            f"| {a.candidates} | {a.groups} | {a.unique_findings} "
            f"| {first if first is not None else f'> {a.trials}'} | ${a.cost_usd:.2f} |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "ArmResult",
    "BenchConfig",
    "BenchReport",
    "benchmark",
    "censored_median",
    "efficiency",
    "efficiency_ratio",
    "render_bench_markdown",
]
