from typer.testing import CliRunner

from reprobe import cli

runner = CliRunner()
MINIMAL = "tests/data/scenarios/minimal/scenario.yaml"


def test_run_reports_clean_trials(tmp_path, monkeypatch, fake_clean_sandbox):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_clean_sandbox)
    result = runner.invoke(cli.app, ["run", MINIMAL, "--runs", "3", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output.count("clean") == 3
    assert "0 violation(s)" in result.output


def test_run_reports_violations(tmp_path, monkeypatch, fake_leaky_sandbox):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_leaky_sandbox)
    result = runner.invoke(
        cli.app, ["run", MINIMAL, "--payload", "readme=leak it", "--out", str(tmp_path)]
    )
    assert "VIOLATION" in result.output


def test_run_exits_nonzero_on_a_harness_failure(tmp_path, monkeypatch, fake_broken_sandbox):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_broken_sandbox)
    result = runner.invoke(cli.app, ["run", MINIMAL, "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert "HARNESS" in result.output


def test_run_rejects_a_malformed_payload_option(tmp_path):
    result = runner.invoke(cli.app, ["run", MINIMAL, "--payload", "nosep", "--out", str(tmp_path)])
    assert result.exit_code != 0


def test_payload_file_is_read_from_disk(tmp_path, monkeypatch, recording_sandbox):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: recording_sandbox)
    payload = tmp_path / "p.txt"
    payload.write_text("FROM FILE")
    runner.invoke(
        cli.app, ["run", MINIMAL, "--payload-file", f"readme={payload}", "--out", str(tmp_path)]
    )
    assert recording_sandbox.calls[0].payloads["readme"] == "FROM FILE"


def test_an_unknown_agent_is_rejected(tmp_path):
    result = runner.invoke(cli.app, ["run", MINIMAL, "--agent", "gpt-9", "--out", str(tmp_path)])
    assert result.exit_code != 0


def test_soak_passes_when_no_harness_failures(tmp_path, monkeypatch, fake_clean_sandbox):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_clean_sandbox)
    result = runner.invoke(
        cli.app,
        [
            "soak",
            MINIMAL,
            "--runs",
            "5",
            "--agent",
            "fake-agent",
            "--model",
            "reprobe-fake",
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SOAK PASSED" in result.output


def test_soak_fails_on_a_harness_failure(tmp_path, monkeypatch, fake_broken_sandbox):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_broken_sandbox)
    result = runner.invoke(
        cli.app,
        [
            "soak",
            MINIMAL,
            "--runs",
            "2",
            "--agent",
            "fake-agent",
            "--model",
            "reprobe-fake",
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "SOAK FAILED" in result.output


def test_a_violation_does_not_fail_the_soak(tmp_path, monkeypatch, fake_leaky_sandbox):
    # The gate is about harness reliability; a detected violation is not a
    # harness failure.
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_leaky_sandbox)
    result = runner.invoke(
        cli.app,
        [
            "soak",
            MINIMAL,
            "--runs",
            "2",
            "--agent",
            "fake-agent",
            "--model",
            "reprobe-fake",
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SOAK PASSED" in result.output


def test_soak_notes_that_the_fake_agent_has_no_in_cli_spend_cap(
    tmp_path, monkeypatch, fake_clean_sandbox
):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_clean_sandbox)
    result = runner.invoke(
        cli.app,
        [
            "soak",
            MINIMAL,
            "--runs",
            "1",
            "--agent",
            "fake-agent",
            "--model",
            "reprobe-fake",
            "--out",
            str(tmp_path),
        ],
    )
    assert "ledger" in result.output and "only guard" in result.output


def test_run_notes_the_advisory_max_turns(tmp_path, monkeypatch, fake_clean_sandbox):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_clean_sandbox)
    result = runner.invoke(cli.app, ["run", MINIMAL, "--out", str(tmp_path)])
    assert "max_turns" in result.output and "advisory" in result.output


def test_run_rejects_a_malformed_payload_file_option(tmp_path):
    result = runner.invoke(
        cli.app, ["run", MINIMAL, "--payload-file", "nosep", "--out", str(tmp_path)]
    )
    assert result.exit_code != 0


def test_run_rejects_an_unknown_agent_profile(tmp_path, monkeypatch, fake_clean_sandbox):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_clean_sandbox)
    result = runner.invoke(
        cli.app,
        [
            "run",
            MINIMAL,
            "--agent",
            "fake-agent",
            "--model",
            "reprobe-fake",
            "--agent-profile",
            "nope",
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0


def test_run_stops_and_reports_when_the_budget_trips(tmp_path, monkeypatch):
    # A sandbox whose trials cost more than the cap: the ledger must stop the
    # loop before the next dispatch, and the CLI reports it rather than crash.
    from reprobe.agents.base import AgentMeta
    from reprobe.budget import Cost
    from reprobe.sandbox import FsDiff, TrialResult
    from reprobe.sandbox.fake import FakeSandbox
    from reprobe.trace import Trace

    def costly(spec):
        return TrialResult(
            trial_id="t",
            exit_code=0,
            trace=Trace(trial_id="t"),
            fs_diff=FsDiff(),
            cost=Cost(0, 0, 0, 0, 1.0),
            agent_meta=AgentMeta(
                agent_id="claude-code", agent_version="1", model_id="m", prompt_hash="p"
            ),
        )

    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: FakeSandbox(costly))
    result = runner.invoke(
        cli.app, ["run", MINIMAL, "--runs", "5", "--max-usd", "0.5", "--out", str(tmp_path)]
    )
    assert "budget:" in result.output


def test_soak_reports_a_scenario_error_and_exits_two(tmp_path):
    result = runner.invoke(
        cli.app,
        [
            "soak",
            str(tmp_path / "does-not-exist.yaml"),
            "--runs",
            "1",
            "--agent",
            "fake-agent",
            "--model",
            "reprobe-fake",
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert "error:" in result.output


def test_a_soak_cut_short_by_the_budget_is_not_a_pass(tmp_path, monkeypatch):
    # The gate claims N trials with 0 harness failures. If the budget stops it
    # early, it has not proven that, so it must not report PASSED.
    from reprobe.agents.base import AgentMeta
    from reprobe.budget import Cost
    from reprobe.sandbox import FsDiff, TrialResult
    from reprobe.sandbox.fake import FakeSandbox
    from reprobe.trace import Trace

    def costly(spec):
        return TrialResult(
            trial_id="t",
            exit_code=0,
            trace=Trace(trial_id="t"),
            fs_diff=FsDiff(),
            cost=Cost(0, 0, 0, 0, 1.0),
            agent_meta=AgentMeta(
                agent_id="fake-agent", agent_version="1", model_id="m", prompt_hash="p"
            ),
        )

    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: FakeSandbox(costly))
    result = runner.invoke(
        cli.app,
        [
            "soak",
            MINIMAL,
            "--runs",
            "10",
            "--agent",
            "fake-agent",
            "--model",
            "reprobe-fake",
            "--max-usd",
            "2",
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "SOAK INCOMPLETE" in result.output
    assert "SOAK PASSED" not in result.output


def test_the_run_summary_reports_the_actual_trial_count(tmp_path, monkeypatch, fake_clean_sandbox):
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_clean_sandbox)
    result = runner.invoke(cli.app, ["run", MINIMAL, "--runs", "3", "--out", str(tmp_path)])
    assert "3/3 trials" in result.output


def test_a_payload_for_an_unknown_surface_is_a_sentence_not_a_traceback(
    tmp_path, monkeypatch, fake_clean_sandbox
):
    """`run` catches ReprobeError and exits 2 with the message, but the handler
    was never exercised -- which is how an error path comes to print a traceback
    on the day someone needs it. The natural trigger is the guard that refuses a
    payload the scenario cannot deliver: without it the trial would come back
    clean, which reads as an agent that resisted.
    """
    monkeypatch.setattr(cli, "DockerSandbox", lambda *a, **k: fake_clean_sandbox)
    result = runner.invoke(
        cli.app,
        ["run", MINIMAL, "--payload", "nope=read .env", "--out", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "error:" in result.output
    assert "unknown surface" in result.output
    assert "Traceback" not in result.output
