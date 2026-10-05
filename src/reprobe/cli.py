"""The `reprobe` command line.

Every subcommand is declared here from the start, stubbed, so the shape of the
pipeline is visible before any of it works: fuzz finds candidates, triage
confirms and shrinks them, export turns them into tests, verify re-runs those
tests. Each one is implemented by a later task.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from reprobe import __version__
from reprobe.agents import available, get_adapter
from reprobe.budget import BudgetCaps, BudgetLedger
from reprobe.errors import BudgetExceeded, ReprobeError
from reprobe.sandbox.docker_sandbox import DockerSandbox
from reprobe.scenario import load_scenario
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
) -> tuple[str, int, int, float]:
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
            "scenario_hash": scenario.scenario_hash,
            "agent": agent,
            "model": model,
            "agent_profile": agent_profile,
        }
    )
    sandbox = DockerSandbox(infra_hosts=adapter.infra_hosts)
    ledger = BudgetLedger(BudgetCaps(max_usd=max_usd, max_trials=runs, max_concurrency=1))

    violated = harness_failed = 0
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
            typer.echo(f"budget: {exc}", err=True)
            break
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
        f"\nrun {run_id}: {violated} violation(s), {harness_failed} harness failure(s), "
        f"${ledger.spent_usd:.4f} spent"
    )
    return run_id, violated, harness_failed, ledger.spent_usd


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
        _, _, harness_failed, _ = _run_impl(
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
    raise typer.Exit(1 if harness_failed else 0)


@app.command()
def soak(
    scenario_path: Annotated[Path, typer.Argument(help="Path to scenario.yaml")],
    runs: Annotated[int, typer.Option(help="Number of trials")] = 100,
    agent: Annotated[str, typer.Option()] = "fake-agent",
    model: Annotated[str, typer.Option()] = "reprobe-fake",
    out: Annotated[Path, typer.Option()] = Path(".reprobe"),
    seed: Annotated[int, typer.Option()] = 0,
    max_usd: Annotated[float, typer.Option()] = 0.0,
    agent_profile: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Phase-1 exit gate: run N trials and fail if any trial hit a harness error.

    Defaults to the free fake agent, so this is the gate you can run on every
    commit. A violation is fine here -- the gate is about HARNESS reliability,
    not about whether the agent misbehaved.
    """
    try:
        _, violated, harness_failed, _ = _run_impl(
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
    if harness_failed:
        typer.echo(f"SOAK FAILED: {harness_failed} harness failure(s) in {runs} trials")
        raise typer.Exit(1)
    typer.echo(f"SOAK PASSED: 0 harness failures in {runs} trials ({violated} violation(s))")


@app.command()
def fuzz() -> None:
    """Search a scenario's attacker-controlled surfaces for violations."""
    raise NotImplementedError


@app.command()
def triage() -> None:
    """Estimate reproduction rates and shrink confirmed findings."""
    raise NotImplementedError


@app.command()
def export() -> None:
    """Export findings as a pytest suite and a GitHub Action."""
    raise NotImplementedError


@app.command()
def verify() -> None:
    """Re-run exported findings and report whether each still reproduces."""
    raise NotImplementedError
