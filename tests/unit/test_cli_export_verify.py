from pathlib import Path

import pytest
from typer.testing import CliRunner

import reprobe.sandbox.docker_sandbox as docker_sandbox
from reprobe import cli
from reprobe.scenario import load_scenario
from reprobe.store import RunStore
from tests.support.findings import make_finding
from tests.support.results import clean_result, leaky_result

runner = CliRunner()
MINIMAL = str(Path("tests/data/scenarios/minimal/scenario.yaml").resolve())
MINIMAL_HASH = load_scenario(MINIMAL).scenario_hash


def _finding(**kw):
    kw.setdefault("scenario_hash", MINIMAL_HASH)
    return make_finding(**kw)


def _store_with(tmp_path, findings, *, record_path=True):
    store = RunStore(tmp_path / "store")
    meta = {"command": "fuzz", "scenario": "minimal"}
    if record_path:
        meta["scenario_path"] = MINIMAL
    run_id = store.open_run(meta)
    for finding in findings:
        store.append(run_id, "findings", finding.to_record())
    return store, run_id


def _export(tmp_path, *args):
    return runner.invoke(
        cli.app, ["export", str(tmp_path / "store"), "--dest", str(tmp_path / "suite"), *args]
    )


@pytest.fixture
def docker(monkeypatch):
    """Stand in for DockerSandbox, which `FindingRunner` builds per agent."""
    state = {"behaviour": lambda spec: clean_result(), "built": 0}

    class Fake:
        def __init__(self, *, infra_hosts):
            state["built"] += 1

        def run(self, spec):
            return state["behaviour"](spec)

        def describe(self):
            return {}

    monkeypatch.setattr(docker_sandbox, "DockerSandbox", Fake)
    return state


def _verify(tmp_path, *args):
    return runner.invoke(
        cli.app,
        [
            "verify",
            str(tmp_path / "suite"),
            "--store",
            str(tmp_path / "verify-store"),
            *args,
        ],
    )


# --- export -------------------------------------------------------------------


def test_export_writes_a_runnable_suite_and_says_what_next(tmp_path):
    _store_with(tmp_path, [_finding()])
    result = _export(tmp_path)
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "suite").glob("test_*.py"))) == 1
    assert "1 test(s)" in result.output
    assert "REPROBE_REGRESSION_TESTS=1" in result.output
    assert "reprobe verify" in result.output
    # Not `uv run pytest`: that works only inside a Reprobe checkout, and an
    # exported suite lives in somebody else's repository.
    assert "uv run" not in result.output
    assert "pytest" in result.output


def test_export_writes_one_test_per_duplicate_group_by_default(tmp_path):
    same = ["canary_read:/workspace/.env"]
    _store_with(tmp_path, [_finding(actions=same), _finding(actions=same, payload="longer one")])
    assert _export(tmp_path).exit_code == 0
    assert len(list((tmp_path / "suite").glob("test_*.py"))) == 1


def test_export_all_writes_every_finding(tmp_path):
    same = ["canary_read:/workspace/.env"]
    _store_with(tmp_path, [_finding(actions=same), _finding(actions=same, payload="longer one")])
    assert _export(tmp_path, "--all").exit_code == 0
    assert len(list((tmp_path / "suite").glob("test_*.py"))) == 2


def test_export_defaults_to_the_latest_run(tmp_path):
    store, _ = _store_with(tmp_path, [_finding(actions=["a"])])
    run_id = store.open_run({"command": "fuzz", "scenario_path": MINIMAL})
    store.append(run_id, "findings", _finding(actions=["b"]).to_record())
    store.append(run_id, "findings", _finding(actions=["c"]).to_record())
    assert _export(tmp_path).exit_code == 0
    assert len(list((tmp_path / "suite").glob("test_*.py"))) == 2


def test_export_reads_a_named_run(tmp_path):
    store, first = _store_with(tmp_path, [_finding(actions=["a"])])
    later = store.open_run({"command": "fuzz", "scenario_path": MINIMAL})
    store.append(later, "findings", _finding(actions=["b"]).to_record())
    store.append(later, "findings", _finding(actions=["c"]).to_record())
    assert _export(tmp_path, "--run", first).exit_code == 0
    assert len(list((tmp_path / "suite").glob("test_*.py"))) == 1


def test_export_with_no_findings_says_so_and_fails(tmp_path):
    _store_with(tmp_path, [])
    result = _export(tmp_path)
    assert result.exit_code == 1
    assert "no findings" in result.output


def test_export_needs_a_scenario_path_from_the_run_or_the_flag(tmp_path):
    _store_with(tmp_path, [_finding()], record_path=False)
    result = _export(tmp_path)
    assert result.exit_code == 2
    assert "--scenario" in result.output
    assert _export(tmp_path, "--scenario", MINIMAL).exit_code == 0


def test_export_reports_a_refusal_without_a_traceback(tmp_path):
    _store_with(tmp_path, [_finding(container_digest="")])
    result = _export(tmp_path)
    assert result.exit_code == 2
    assert "not pinned" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _export(tmp_path, "--allow-unpinned").exit_code == 0


def test_export_passes_the_trial_cap_through_and_refuses_an_impossible_one(tmp_path):
    _store_with(tmp_path, [_finding()])
    assert _export(tmp_path, "--max-trials", "8").exit_code == 2
    assert _export(tmp_path, "--max-trials", "30").exit_code == 0
    test_file = next((tmp_path / "suite").glob("test_*.py"))
    assert "MAX_TRIALS = 30" in test_file.read_text()


# --- verify -------------------------------------------------------------------


def _exported(tmp_path, *findings):
    _store_with(tmp_path, list(findings) or [_finding()])
    assert _export(tmp_path, "--all").exit_code == 0


def test_verify_exits_1_while_the_exploit_reproduces(tmp_path, docker):
    _exported(tmp_path)
    docker["behaviour"] = lambda spec: leaky_result(canary=spec.canaries[0].value)
    result = _verify(tmp_path)
    assert result.exit_code == 1, result.output
    assert "REPRODUCES" in result.output
    assert "1 of 1 finding(s) not fixed" in result.output


def test_verify_exits_0_once_every_finding_is_fixed(tmp_path, docker):
    _exported(tmp_path)
    result = _verify(tmp_path)
    assert result.exit_code == 0, result.output
    assert "FIXED" in result.output
    assert "0 of 1 finding(s) not fixed" in result.output


def test_verify_prints_pin_drift(tmp_path, docker):
    _exported(tmp_path, _finding(agent_version="9.9.9"))
    result = _verify(tmp_path)
    assert "agent version changed: recorded 9.9.9" in result.output


def test_verify_reports_a_finding_it_cannot_measure_and_keeps_going(tmp_path, docker):
    """A scenario edited after export is a failure for that finding, not a
    crash that hides the verdict on every other one."""
    _exported(tmp_path, _finding(actions=["a"]), _finding(actions=["b"]))
    bundled = tmp_path / "suite" / "reprobe_findings" / "scenarios" / "minimal"
    (bundled / "fixture" / "README.md").write_text("edited after export\n")
    result = _verify(tmp_path)
    assert result.exit_code == 1, result.output
    assert result.output.count("ERROR") == 2
    assert "scenario changed" in result.output


def test_verify_refuses_an_undecidable_trial_cap_before_any_trial(tmp_path, docker):
    _exported(tmp_path)
    calls = {"n": 0}

    def behaviour(spec):
        calls["n"] += 1
        return clean_result()

    docker["behaviour"] = behaviour
    result = _verify(tmp_path, "--max-trials", "8")
    assert result.exit_code == 2
    assert "fixed agent could never pass" in result.output
    assert calls["n"] == 0


def test_verify_honours_a_trial_cap_override(tmp_path, docker):
    _exported(tmp_path)
    calls = {"n": 0}

    def straddling(spec):  # 1 in 3: never decisive either way at 12 trials
        calls["n"] += 1
        if calls["n"] % 3 == 1:
            return leaky_result(canary=spec.canaries[0].value)
        return clean_result()

    docker["behaviour"] = straddling
    result = _verify(tmp_path, "--max-trials", "12")
    assert "INCONCLUSIVE" in result.output
    assert calls["n"] == 12


def test_verify_takes_a_fake_profile_override_from_the_environment(tmp_path, docker):
    _exported(tmp_path, _finding(agent_id="fake-agent", agent_profile="always"))
    seen = []

    def behaviour(spec):
        seen.append(spec.env_overrides["REPROBE_FAKE_PROFILE"])
        return clean_result()

    docker["behaviour"] = behaviour
    result = runner.invoke(
        cli.app,
        ["verify", str(tmp_path / "suite"), "--store", str(tmp_path / "vs")],
        env={"REPROBE_FAKE_PROFILE_OVERRIDE": "never"},
    )
    assert result.exit_code == 0, result.output
    from reprobe.agents.fake_agent import FakeAgentAdapter

    never = FakeAgentAdapter.profile_env("never", 0, canary_path=".env")["REPROBE_FAKE_PROFILE"]
    assert seen and set(seen) == {never}


def test_verify_without_a_suite_is_a_usage_error(tmp_path):
    result = runner.invoke(cli.app, ["verify", str(tmp_path)])
    assert result.exit_code == 2
    assert "no exported findings" in result.output


def test_verify_counts_dollars_across_the_whole_suite(tmp_path, docker):
    _exported(tmp_path, _finding(actions=["a"]), _finding(actions=["b"]))
    docker["behaviour"] = lambda spec: leaky_result(canary=spec.canaries[0].value, usd=1.0)
    result = _verify(tmp_path, "--max-usd", "2.5")
    assert result.exit_code == 1
    assert "INCONCLUSIVE" in result.output
    assert "budget" in result.output


def test_export_from_an_empty_store_says_so_without_a_traceback(tmp_path):
    result = _export(tmp_path)
    assert result.exit_code == 2
    assert "no runs" in result.output
    assert isinstance(result.exception, SystemExit)


def test_export_of_an_unknown_run_says_so_without_a_traceback(tmp_path):
    _store_with(tmp_path, [_finding()])
    result = _export(tmp_path, "--run", "run_nope")
    assert result.exit_code == 2
    assert "run_nope" in result.output
    assert isinstance(result.exception, SystemExit)


# --- export --workflow ----------------------------------------------------------


def test_export_writes_a_workflow_only_when_asked(tmp_path, monkeypatch):
    """A scheduled workflow spends real agent budget every week. Writing one
    into somebody's .github unasked is not a default."""
    monkeypatch.chdir(tmp_path)
    _store_with(tmp_path, [_finding()])
    assert runner.invoke(cli.app, ["export", "store", "--dest", "suite"]).exit_code == 0
    assert not (tmp_path / ".github").exists()


def test_export_writes_a_workflow_that_runs_the_suite_it_just_wrote(tmp_path, monkeypatch):
    import yaml

    monkeypatch.chdir(tmp_path)
    _store_with(tmp_path, [_finding()])
    result = runner.invoke(
        cli.app,
        [
            "export",
            "store",
            "--dest",
            str(tmp_path / "tests" / "reprobe"),
            "--workflow",
            ".github/workflows/reprobe.yml",
            "--reprobe-ref",
            "v0.1.0",
            "--schedule",
            "0 3 * * *",
        ],
    )
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load((tmp_path / ".github/workflows/reprobe.yml").read_text())
    job = doc["jobs"]["reprobe"]
    assert job["env"]["REPROBE_SUITE"] == "tests/reprobe"  # relative to the repo root
    assert doc[True]["schedule"] == [{"cron": "0 3 * * *"}]
    assert "workflow" in result.output


def test_export_refuses_a_suite_outside_the_repository(tmp_path, monkeypatch):
    """The workflow runs from the repository root; a suite elsewhere is a path
    the checkout will never contain."""
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    _store_with(tmp_path, [_finding()])
    result = runner.invoke(
        cli.app,
        [
            "export",
            str(tmp_path / "store"),
            "--dest",
            str(tmp_path / "elsewhere"),
            "--workflow",
            ".github/workflows/reprobe.yml",
            "--reprobe-ref",
            "v0.1.0",
        ],
    )
    assert result.exit_code == 2
    assert "inside the repository" in result.output
    assert not (tmp_path / "elsewhere").exists()


def test_export_reports_a_workflow_refusal_without_a_traceback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _store_with(tmp_path, [_finding()])
    result = runner.invoke(
        cli.app,
        [
            "export",
            "store",
            "--dest",
            "suite",
            "--workflow",
            ".github/workflows/r.yml",
            "--reprobe-ref",
            "bad ref",
        ],
    )
    assert result.exit_code == 2
    assert "ref" in result.output
    assert isinstance(result.exception, SystemExit)


def test_verify_keeps_going_past_a_finding_for_an_agent_it_does_not_know(tmp_path, docker):
    """A raw KeyError used to abort the run with no summary, reproduced."""
    import json as _json

    _exported(tmp_path, _finding(actions=["a"]), _finding(actions=["b"]))
    record = next((tmp_path / "suite" / "reprobe_findings").glob("*.json"))
    data = _json.loads(record.read_text())
    data["agent_meta"]["agent_id"] = "nope"
    record.write_text(_json.dumps(data))
    result = _verify(tmp_path)
    assert result.exit_code == 1, result.output
    assert result.output.count("ERROR") == 1
    assert "of 2 finding(s) not fixed" in result.output
    assert isinstance(result.exception, SystemExit)


def test_verify_refuses_an_unknown_profile_override_before_any_trial(tmp_path, docker):
    _exported(tmp_path, _finding(agent_id="fake-agent", agent_profile="always"))
    calls = {"n": 0}

    def behaviour(spec):
        calls["n"] += 1
        return clean_result()

    docker["behaviour"] = behaviour
    result = _verify(tmp_path, "--fake-profile-override", "nevr")
    assert result.exit_code == 2
    assert "nevr" in result.output
    assert calls["n"] == 0


def test_verify_on_a_mangled_suite_is_a_usage_error(tmp_path, docker):
    _exported(tmp_path)
    test_file = next((tmp_path / "suite").glob("test_*.py"))
    test_file.write_text(test_file.read_text().replace("FINGERPRINT = ", "NOT_IT = "))
    result = _verify(tmp_path)
    assert result.exit_code == 2
    assert test_file.name in result.output
    assert isinstance(result.exception, SystemExit)


def test_verify_and_the_exported_suite_share_a_default_budget():
    """`reprobe verify` defaulted to $10 while the suite's conftest used $5."""
    from typer.main import get_command

    verify = get_command(cli.app).commands["verify"]  # type: ignore[attr-defined]
    default = next(p.default for p in verify.params if p.name == "max_usd")
    from reprobe.export.pytest_export import render_conftest

    assert f'"REPROBE_MAX_USD", "{default:g}"' in render_conftest([_finding()])
