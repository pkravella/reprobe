from typer.testing import CliRunner

from reprobe import __version__
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
