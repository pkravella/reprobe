"""The `reprobe` command line.

Every subcommand is declared here from the start, stubbed, so the shape of the
pipeline is visible before any of it works: fuzz finds candidates, triage
confirms and shrinks them, export turns them into tests, verify re-runs those
tests. Each one is implemented by a later task.
"""

from __future__ import annotations

import typer

from reprobe import __version__

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


@app.command()
def run() -> None:
    """Run one trial of one scenario."""
    raise NotImplementedError


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
