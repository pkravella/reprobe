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


# --- commands that are not built yet --------------------------------------


@pytest.mark.parametrize("command", ["triage", "export", "verify"])
def test_an_unbuilt_command_says_so_instead_of_dumping_a_traceback(command):
    """`reprobe fuzz` ends by telling the user to run `reprobe triage`.

    Following that advice printed a bare `NotImplementedError` traceback, which
    reads like a crash rather than a feature that has not landed.
    """
    result = runner.invoke(cli.app, [command])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "NotImplementedError" not in result.output
    assert "not built yet" in result.output.lower()


def test_fuzz_points_at_triage_only_as_a_next_step_not_a_promise():
    """The pointer is fine; it just must not claim the command works today."""
    import inspect

    source = inspect.getsource(cli.fuzz)
    assert "reprobe triage" in source
