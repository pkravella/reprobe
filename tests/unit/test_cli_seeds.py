from typer.testing import CliRunner

from reprobe import cli
from reprobe.seeds import load_seeds

runner = CliRunner()


def _import(*args):
    return runner.invoke(cli.app, ["seeds", "import", *args])


def test_seeds_import_writes_a_loadable_file_and_counts_it(tmp_path):
    dest = tmp_path / "imported.yaml"
    result = _import("tests/data/agentdojo-sample", "--format", "agentdojo", "--out", str(dest))
    assert result.exit_code == 0, result.output
    assert "imported 2 seed(s)" in result.output
    assert len(load_seeds([dest])) == 24 + 2


def test_seeds_import_says_loudly_how_many_are_untargeted(tmp_path):
    """Agreed: verbatim import, but a search must not spend budget on seeds
    that cannot aim at the canary without anyone being told."""
    result = _import(
        "tests/data/promptfoo-sample.yaml",
        "--format",
        "promptfoo",
        "--out",
        str(tmp_path / "s.yaml"),
    )
    assert result.exit_code == 0, result.output
    assert "untargeted" in result.output
    assert "{{ canary_path }}" in result.output


def test_seeds_import_unknown_format_is_a_usage_error(tmp_path):
    result = _import("x", "--format", "garak", "--out", str(tmp_path / "s.yaml"))
    assert result.exit_code == 2
    assert "garak" in result.output


def test_seeds_import_bad_input_is_a_clean_error(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("just a string")
    result = _import(str(bad), "--format", "promptfoo", "--out", str(tmp_path / "s.yaml"))
    assert result.exit_code == 2
    assert "promptfoo" in result.output
    assert isinstance(result.exception, SystemExit)


def test_seeds_import_will_not_overwrite_without_force(tmp_path):
    dest = tmp_path / "s.yaml"
    args = ["tests/data/agentdojo-sample", "--format", "agentdojo", "--out", str(dest)]
    assert _import(*args).exit_code == 0
    again = _import(*args)
    assert again.exit_code == 2
    assert "--force" in again.output
    assert _import(*args, "--force").exit_code == 0


def test_the_force_hint_only_follows_the_refusal_to_overwrite(tmp_path):
    """It was appended to any error once the file existed, so a malformed
    input was answered with "pass --force"."""
    dest = tmp_path / "s.yaml"
    dest.write_text("[]\n")
    bad = tmp_path / "bad.yaml"
    bad.write_text("just a string")
    result = _import(str(bad), "--format", "promptfoo", "--out", str(dest))
    assert result.exit_code == 2
    assert "--force" not in result.output


def test_an_unwritable_destination_is_a_clean_error(tmp_path):
    (tmp_path / "dir").mkdir()
    result = _import(
        "tests/data/agentdojo-sample",
        "--format",
        "agentdojo",
        "--out",
        str(tmp_path / "dir"),
        "--force",
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
