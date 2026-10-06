from typer.testing import CliRunner

from reprobe import cli

runner = CliRunner()
MINIMAL = "tests/data/scenarios/minimal/scenario.yaml"


def _invoke(tmp_path, monkeypatch, sandbox, *args):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: sandbox)
    return runner.invoke(cli.app, ["fuzz", MINIMAL, "--out", str(tmp_path), *args])


def test_fuzz_reports_a_summary(tmp_path, monkeypatch, fake_clean_sandbox):
    out = _invoke(tmp_path, monkeypatch, fake_clean_sandbox, "--trials", "5")
    assert out.exit_code == 0, out.output
    for line in ("trials", "candidates", "harness failures", "corpus", "covered edges", "cost"):
        assert line in out.output
    assert "reprobe triage" in out.output


def test_fuzz_reports_candidates_when_it_finds_them(tmp_path, monkeypatch, fake_leaky_sandbox):
    out = _invoke(tmp_path, monkeypatch, fake_leaky_sandbox, "--trials", "5")
    assert out.exit_code == 0, out.output
    # Every trial violates and each carries a different payload, so each is its
    # own candidate. Collapsing them is Phase 3's job, by violating action.
    assert "candidates        5" in out.output


def test_the_random_scheduler_is_reachable_from_the_flag(tmp_path, monkeypatch, fake_clean_sandbox):
    out = _invoke(
        tmp_path, monkeypatch, fake_clean_sandbox, "--trials", "4", "--scheduler", "random"
    )
    assert out.exit_code == 0, out.output
    assert "random" in out.output


def test_an_unknown_scheduler_is_a_clean_error_not_a_traceback(
    tmp_path, monkeypatch, fake_clean_sandbox
):
    out = _invoke(tmp_path, monkeypatch, fake_clean_sandbox, "--scheduler", "bogus")
    assert out.exit_code != 0
    assert "bogus" in out.output
    assert "Traceback" not in out.output


def test_an_unknown_coverage_group_is_a_clean_error(tmp_path, monkeypatch, fake_clean_sandbox):
    out = _invoke(tmp_path, monkeypatch, fake_clean_sandbox, "--coverage-groups", "tool_ngrams")
    assert out.exit_code != 0
    assert "tool_ngrams" in out.output
    assert "Traceback" not in out.output


def test_an_unknown_agent_is_a_clean_error(tmp_path, monkeypatch, fake_clean_sandbox):
    out = _invoke(tmp_path, monkeypatch, fake_clean_sandbox, "--agent", "nope")
    assert out.exit_code != 0
    assert "Traceback" not in out.output


def test_the_free_lane_runs_from_the_cli(tmp_path, monkeypatch, fake_clean_sandbox):
    """The Phase-2 gate's invocation: fake agent, a named profile, zero dollars."""
    out = _invoke(
        tmp_path,
        monkeypatch,
        fake_clean_sandbox,
        "--agent",
        "fake-agent",
        "--model",
        "reprobe-fake",
        "--agent-profile",
        "placement_sensitive",
        "--max-usd",
        "0",
        "--trials",
        "4",
    )
    assert out.exit_code == 0, out.output
    assert "cost              $0.0000" in out.output


def test_a_zero_dollar_cap_does_not_block_a_free_agent(tmp_path, monkeypatch, fake_clean_sandbox):
    """`--max-usd 0` means "this must cost nothing", not "dispatch nothing"."""
    out = _invoke(
        tmp_path,
        monkeypatch,
        fake_clean_sandbox,
        "--agent",
        "fake-agent",
        "--model",
        "reprobe-fake",
        "--max-usd",
        "0",
        "--trials",
        "6",
    )
    assert "trials            6" in out.output
