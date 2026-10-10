"""The `reprobe` command line.

Every subcommand is declared here from the start, stubbed, so the shape of the
pipeline is visible before any of it works: fuzz finds candidates, triage
confirms and shrinks them, export turns them into tests, verify re-runs those
tests. Each one is implemented by a later task.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NamedTuple

import typer
from pydantic import ValidationError

from reprobe import __version__
from reprobe.agents import available, get_adapter
from reprobe.budget import BudgetCaps, BudgetLedger
from reprobe.coverage import SIGNAL_GROUPS
from reprobe.errors import BudgetExceeded, ReprobeError
from reprobe.sandbox.docker_sandbox import DockerSandbox
from reprobe.scenario import load_scenario
from reprobe.stats import DEFAULT_THRESHOLD
from reprobe.store import RunStore
from reprobe.trial import known_profiles, run_trial

app = typer.Typer(
    name="reprobe",
    help="Turn an agent security failure into a reproducible regression test.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"reprobe {__version__}")
        raise typer.Exit


@app.callback()
def main(
    # Unused in the body on purpose: the eager callback handles `--version` and
    # exits before this function runs. Typer needs the parameter to register it.
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version.",
    ),
) -> None:
    """Reprobe CLI."""


class _RunSummary(NamedTuple):
    run_id: str
    completed: int
    violated: int
    harness_failed: int
    spent_usd: float


def _parse_payloads(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        surface, sep, value = pair.partition("=")
        if not sep:
            raise typer.BadParameter(f"--payload must be surface=text, got {pair!r}")
        out[surface] = value
    return out


def _run_impl(
    scenario_path: Path,
    *,
    agent: str,
    model: str,
    payloads: dict[str, str],
    runs: int,
    out: Path,
    seed: int,
    max_usd: float,
    otel_endpoint: str | None,
    agent_profile: str | None,
) -> _RunSummary:
    """Run `runs` trials of one scenario. Returns (run_id, violations, harness_failures, spent)."""
    scenario = load_scenario(scenario_path)
    adapter = get_adapter(agent)  # raises KeyError with the known ids on a typo
    if agent_profile is not None and agent_profile not in known_profiles():
        raise typer.BadParameter(
            f"unknown --agent-profile {agent_profile!r}; known: {known_profiles()}"
        )

    # Two advisories, because a limit that silently does nothing is worse than
    # no limit (the scenario author believes a bound exists):
    if not adapter.enforces_max_usd:
        typer.echo(
            f"note: {agent} has no in-CLI spend cap; the budget ledger "
            f"(--max-usd {max_usd}) is the only guard.",
            err=True,
        )
    if scenario.limits.max_turns:
        typer.echo(
            f"note: scenario sets max_turns={scenario.limits.max_turns}, which no agent CLI "
            "enforces; it is advisory. The per-trial dollar cap is what bounds a trial.",
            err=True,
        )

    store = RunStore(out)
    run_id = store.open_run(
        {
            "command": "run",
            "scenario": scenario.name,
            "scenario_path": str(scenario_path.resolve()),
            "scenario_hash": scenario.scenario_hash,
            "agent": agent,
            "model": model,
            "agent_profile": agent_profile,
        }
    )
    sandbox = DockerSandbox(infra_hosts=adapter.infra_hosts)
    ledger = BudgetLedger(BudgetCaps(max_usd=max_usd, max_trials=runs, max_concurrency=1))

    violated = harness_failed = completed = 0
    for i in range(runs):
        try:
            record = run_trial(
                scenario,
                agent_id=agent,
                model=model,
                payloads=payloads,
                sandbox=sandbox,
                store=store,
                run_id=run_id,
                seed=seed + i,
                ledger=ledger,
                otel_endpoint=otel_endpoint,
                agent_profile=agent_profile,
                infra_hosts=adapter.infra_hosts,
            )
        except BudgetExceeded as exc:
            typer.echo(f"budget: {exc} (ran {completed} of {runs})", err=True)
            break
        completed += 1
        if record.harness_error:
            harness_failed += 1
            typer.echo(f"[{i + 1}/{runs}] HARNESS {record.harness_error}")
        elif record.violated:
            violated += 1
            assert record.verdict is not None
            typer.echo(f"[{i + 1}/{runs}] VIOLATION {record.verdict.action_keys}")
        else:
            typer.echo(f"[{i + 1}/{runs}] clean")

    typer.echo(
        f"\nrun {run_id}: {completed}/{runs} trials, {violated} violation(s), "
        f"{harness_failed} harness failure(s), ${ledger.spent_usd:.4f} spent"
    )
    return _RunSummary(run_id, completed, violated, harness_failed, ledger.spent_usd)


@app.command()
def run(
    scenario_path: Annotated[Path, typer.Argument(help="Path to scenario.yaml")],
    agent: Annotated[str, typer.Option(help=f"One of: {', '.join(available())}")] = "claude-code",
    model: Annotated[str, typer.Option(help="Model id passed to the agent")] = "claude-haiku-4-5",
    payload: Annotated[list[str] | None, typer.Option(help="surface=text")] = None,
    payload_file: Annotated[list[str] | None, typer.Option(help="surface=path")] = None,
    runs: Annotated[int, typer.Option(help="Repeat the same trial N times")] = 1,
    out: Annotated[Path, typer.Option(help="Run store directory")] = Path(".reprobe"),
    seed: Annotated[int, typer.Option(help="Base seed; trial i uses seed+i")] = 0,
    max_usd: Annotated[float, typer.Option(help="Dollar cap for this invocation")] = 5.0,
    agent_profile: Annotated[str | None, typer.Option(help="Fake-agent behaviour profile")] = None,
    otel_endpoint: Annotated[str | None, typer.Option(help="OTLP/HTTP endpoint")] = None,
) -> None:
    """Run one scenario with fixed payloads, `runs` times."""
    payloads = _parse_payloads(payload)
    for pair in payload_file or []:
        surface, sep, path = pair.partition("=")
        if not sep:
            raise typer.BadParameter(f"--payload-file must be surface=path, got {pair!r}")
        payloads[surface] = Path(path).read_text()

    try:
        summary = _run_impl(
            scenario_path,
            agent=agent,
            model=model,
            payloads=payloads,
            runs=runs,
            out=out,
            seed=seed,
            max_usd=max_usd,
            otel_endpoint=otel_endpoint,
            agent_profile=agent_profile,
        )
    except ReprobeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    raise typer.Exit(1 if summary.harness_failed else 0)


@app.command()
def soak(
    scenario_path: Annotated[Path, typer.Argument(help="Path to scenario.yaml")],
    runs: Annotated[int, typer.Option(help="Number of trials")] = 100,
    agent: Annotated[str, typer.Option()] = "fake-agent",
    model: Annotated[str, typer.Option()] = "reprobe-fake",
    out: Annotated[Path, typer.Option()] = Path(".reprobe"),
    seed: Annotated[int, typer.Option()] = 0,
    max_usd: Annotated[
        float, typer.Option(help="Dollar cap; the default of 0 means it must cost nothing")
    ] = 0.0,
    agent_profile: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Phase-1 exit gate: run N trials and fail if any trial hit a harness error.

    Defaults to the free fake agent, so this is the gate you can run on every
    commit. A violation is fine here -- the gate is about HARNESS reliability,
    not about whether the agent misbehaved.
    """
    try:
        summary = _run_impl(
            scenario_path,
            agent=agent,
            model=model,
            payloads={},
            runs=runs,
            out=out,
            seed=seed,
            max_usd=max_usd,
            otel_endpoint=None,
            agent_profile=agent_profile,
        )
    except ReprobeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    # The gate requires BOTH zero harness failures AND that every requested
    # trial actually ran -- a soak cut short by the budget has not proven
    # anything, so it must not report PASSED.
    if summary.harness_failed:
        typer.echo(
            f"SOAK FAILED: {summary.harness_failed} harness failure(s) "
            f"in {summary.completed}/{runs} trials"
        )
        raise typer.Exit(1)
    if summary.completed < runs:
        typer.echo(
            f"SOAK INCOMPLETE: only {summary.completed}/{runs} trials ran "
            "(budget stopped it); not a pass"
        )
        raise typer.Exit(1)
    typer.echo(
        f"SOAK PASSED: 0 harness failures in {runs} trials ({summary.violated} violation(s))"
    )


@app.command()
def fuzz(
    scenario_path: Annotated[Path, typer.Argument(help="Path to scenario.yaml")],
    agent: Annotated[str, typer.Option(help=f"One of: {', '.join(available())}")] = "claude-code",
    model: Annotated[str, typer.Option(help="Model id passed to the agent")] = "claude-haiku-4-5",
    scheduler: Annotated[str, typer.Option(help="energy | random")] = "energy",
    trials: Annotated[int, typer.Option(help="Trial cap for this run")] = 100,
    max_usd: Annotated[float, typer.Option(help="Dollar cap; 0 means it must cost nothing")] = 20.0,
    concurrency: Annotated[int, typer.Option(help="Trials in flight at once")] = 4,
    seed: Annotated[int, typer.Option(help="Replays the whole search")] = 0,
    coverage_groups: Annotated[str, typer.Option(help="Comma-separated signal groups")] = ",".join(
        SIGNAL_GROUPS
    ),
    baseline_mutations: Annotated[
        int, typer.Option(help="Mutations per candidate for --scheduler random")
    ] = 1,
    agent_profile: Annotated[str | None, typer.Option(help="Fake-agent behaviour profile")] = None,
    otel_endpoint: Annotated[str | None, typer.Option(help="OTLP/HTTP endpoint")] = None,
    out: Annotated[Path, typer.Option(help="Run store directory")] = Path(".reprobe"),
) -> None:
    """Search a scenario's attacker-controlled surfaces for violations."""
    from reprobe.loop import FuzzConfig
    from reprobe.loop import fuzz as run_fuzz

    try:
        scenario = load_scenario(scenario_path)
        adapter = get_adapter(agent)
        config = FuzzConfig(
            scenario=scenario,
            scenario_path=scenario_path.resolve(),
            agent_id=agent,
            model=model,
            # Validated inside `fuzz`, which names the known values; the
            # Literal annotation would otherwise turn a typo into a pydantic
            # traceback instead of a sentence.
            scheduler=scheduler,  # type: ignore[arg-type]
            seed=seed,
            caps=BudgetCaps(max_usd=max_usd, max_trials=trials, max_concurrency=concurrency),
            coverage_groups=tuple(g.strip() for g in coverage_groups.split(",") if g.strip()),
            baseline_mutations=baseline_mutations,
            agent_profile=agent_profile,
            otel_endpoint=otel_endpoint,
        )
        result = run_fuzz(
            config, sandbox=DockerSandbox(infra_hosts=adapter.infra_hosts), store=RunStore(out)
        )
    except (ReprobeError, ValidationError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except KeyError as exc:
        typer.echo(f"error: unknown agent {agent!r}; known: {available()}", err=True)
        raise typer.Exit(2) from exc

    stats = result.scheduler_stats
    typer.echo(
        f"\nrun {result.run_id}\n"
        f"  scheduler         {stats.get('scheduler')}\n"
        f"  trials            {result.trials}\n"
        f"  violating trials  {result.violating_trials}\n"
        f"  candidates        {len(result.violations)}\n"
        f"  harness failures  {result.harness_failures}\n"
        f"  corpus            {stats.get('corpus_size')}\n"
        f"  covered edges     {stats.get('covered_edges')}\n"
        f"  cost              ${result.cost_usd:.4f}\n"
        f"  wall              {result.wall_seconds:.0f}s\n\n"
        f"next (Phase 3): reprobe triage {out} --run {result.run_id}"
    )


def _not_built_yet(command: str, phase: str) -> None:
    """Exit cleanly for a command that is declared but not implemented.

    Every subcommand is declared from the start so the shape of the pipeline is
    visible, and `reprobe fuzz` ends by pointing at `reprobe triage`. Following
    that pointer used to print a bare `NotImplementedError` traceback, which
    reads like a crash rather than a feature that has not landed.
    """
    typer.echo(
        f"error: `reprobe {command}` is not built yet; it lands in {phase}.\n"
        "       Run `reprobe --help` to see what works today.",
        err=True,
    )
    raise typer.Exit(2)


@app.command()
def triage(
    out: Annotated[Path, typer.Argument(help="Run store directory")],
    run: Annotated[str | None, typer.Option(help="Run id; default is the latest")] = None,
    scenario_path: Annotated[
        Path | None,
        typer.Option("--scenario", help="Overrides the path recorded by the run"),
    ] = None,
    agent: Annotated[str, typer.Option(help=f"One of: {', '.join(available())}")] = "claude-code",
    model: Annotated[str, typer.Option(help="Model id passed to the agent")] = "claude-haiku-4-5",
    threshold: Annotated[
        float, typer.Option(help="Lower bound of the reproduction rate a finding must clear")
    ] = DEFAULT_THRESHOLD,
    min_trials: Annotated[int, typer.Option(help="Trials before a rate may be called")] = 5,
    max_trials: Annotated[int, typer.Option(help="Trials per confirmation at most")] = 40,
    max_usd: Annotated[float, typer.Option(help="Dollar cap; 0 means it must cost nothing")] = 50.0,
    shrink_env: Annotated[
        bool, typer.Option("--shrink-env/--no-shrink-env", help="Also narrow the scenario")
    ] = True,
    agent_profile: Annotated[str | None, typer.Option(help="Fake-agent behaviour profile")] = None,
) -> None:
    """Confirm reproduction rates and shrink every candidate from a run."""
    from reprobe.triage import TriageConfig
    from reprobe.triage import triage as run_triage

    adapter = get_adapter(agent)
    if agent_profile is not None and agent_profile not in known_profiles():
        raise typer.BadParameter(f"unknown profile {agent_profile!r}; known: {known_profiles()}")

    store = RunStore(out)
    run_id = run or store.latest_run()
    meta = store.meta(run_id)
    recorded = meta.get("scenario_path")
    if scenario_path is None and not recorded:
        # Older runs, and runs opened by a caller that did not record it. Saying
        # so beats loading some default scenario and triaging against an
        # environment the candidates were never found in.
        typer.echo(
            f"error: run {run_id} records no scenario path; pass --scenario explicitly",
            err=True,
        )
        raise typer.Exit(2)
    scenario = load_scenario(scenario_path or Path(str(recorded)))

    report = run_triage(
        run_id,
        store=store,
        scenario=scenario,
        agent_id=agent,
        model=model,
        sandbox=DockerSandbox(infra_hosts=adapter.infra_hosts),
        config=TriageConfig(
            threshold=threshold,
            min_trials=min_trials,
            max_trials=max_trials,
            caps=BudgetCaps(max_usd=max_usd, max_trials=100_000, max_concurrency=1),
            shrink_env=shrink_env,
            agent_profile=agent_profile,
            infra_hosts=adapter.infra_hosts,
        ),
    )

    for group in report.groups:
        finding = group.representative
        typer.echo(
            f"  {finding.title()}  -{finding.reduction:.0%} bytes"
            f"  ({group.size} candidate(s), {group.distinct_payloads} distinct payload(s), "
            f"{len(group.coverage_signatures)} route(s))"
        )
        if finding.env_removed:
            typer.echo(f"      {finding.env_removed_summary()}")
    for drop in report.dropped:
        typer.echo(f"  dropped {drop.candidate_id}: {drop.reason}")

    typer.echo(
        f"\n{len(report.findings)} finding(s) in {len(report.groups)} group(s), "
        f"{len(report.dropped)} dropped, median reduction "
        f"{report.median_reduction:.0%}, ${report.cost_usd:.2f} spent\n"
        f"next: reprobe export {out} --run {run_id}"
    )


@app.command()
def export() -> None:
    """Export findings as a pytest suite and a GitHub Action."""
    _not_built_yet("export", "Phase 4")


@app.command()
def verify() -> None:
    """Re-run exported findings and report whether each still reproduces."""
    _not_built_yet("verify", "Phase 4")
