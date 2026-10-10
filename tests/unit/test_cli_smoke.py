import pytest
from typer.testing import CliRunner

from reprobe import __version__, cli
from reprobe.cli import app

runner = CliRunner()


def test_version_flag_prints_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_bare_invocation_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "fuzz", "triage", "export", "verify"):
        assert command in result.stdout


# --- every declared command is built ----------------------------------------


@pytest.mark.parametrize("command", ["triage", "export", "verify"])
def test_no_command_claims_to_be_unbuilt(command):
    """Each landed after being declared as a stub. Invoked with no arguments it
    must fail on the missing argument, not on being unimplemented."""
    result = runner.invoke(cli.app, [command])
    assert result.exit_code != 0
    assert "not built yet" not in result.output.lower()
    assert "Traceback" not in result.output


def test_fuzz_points_at_triage_as_the_next_step():
    import inspect

    source = inspect.getsource(cli.fuzz)
    assert "reprobe triage" in source
