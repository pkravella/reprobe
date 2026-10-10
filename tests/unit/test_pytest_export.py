import ast
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reprobe.errors import ConfigError
from reprobe.export.pytest_export import (
    FINDINGS_DIR,
    OPT_IN_ENV,
    render_conftest,
    render_test,
    suite_entries,
    write_suite,
)
from reprobe.scenario import load_scenario
from reprobe.verify import Outcome, Status, load_finding
from tests.support.findings import make_finding as _make_finding

MINIMAL = Path("tests/data/scenarios/minimal/scenario.yaml").resolve()
MINIMAL_HASH = load_scenario(MINIMAL).scenario_hash


def make_finding(**kw):
    """Findings whose scenario hash is the real one, so a suite can bundle it."""
    kw.setdefault("scenario_hash", MINIMAL_HASH)
    return _make_finding(**kw)


def _suite(findings, dest, **kw):
    kw.setdefault("scenarios", {"minimal": MINIMAL})
    return write_suite(findings, dest, **kw)


def _assigned(source, name):
    """The literal value a generated module assigns to `name`."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not assigned in the generated module")


# --- rendering ----------------------------------------------------------------


def test_rendered_test_is_valid_python():
    ast.parse(render_test(make_finding()))


def test_rendered_conftest_is_valid_python():
    ast.parse(render_conftest([make_finding()]))


def test_rendered_test_pins_agent_model_and_container():
    finding = make_finding(
        agent_version="2.1.7", model_id="claude-sonnet-5-5", container_digest="sha256:abc123"
    )
    source = render_test(finding)
    assert "2.1.7" in source
    assert "claude-sonnet-5-5" in source
    assert "sha256:abc123" in source


def test_rendered_test_pins_the_scenario_hash():
    finding = make_finding()
    assert finding.scenario_hash in render_test(finding)


def test_the_decision_parameters_live_in_the_test_source():
    """Threshold and trial cap are what the test *means*, so they sit where a
    reviewer reads them, not in the JSON beside it."""
    finding = make_finding(threshold=0.4)
    source = render_test(finding, max_trials=30)
    assert _assigned(source, "THRESHOLD") == 0.4
    assert _assigned(source, "MAX_TRIALS") == 30
    assert _assigned(source, "FINGERPRINT") == finding.fingerprint()


def test_the_test_names_its_finding_file_by_fingerprint():
    finding = make_finding()
    hexpart = finding.fingerprint().split(":", 1)[1]
    assert f"{hexpart}.json" in render_test(finding)


def test_rendered_test_records_the_reproduction_statistics_as_documentation():
    source = render_test(make_finding(successes=12, trials=20))
    assert "12/20" in source


def test_rendered_test_states_the_claim_is_one_sided():
    """`reprobe.stats`'s caveat, carried into the file someone else will read: the
    interval is not a calibrated 95% interval, and the test passes only when the
    whole of it sits below the threshold."""
    doc = ast.get_docstring(ast.parse(render_test(make_finding())))
    assert "upper bound" in doc
    assert "lower bound" in doc
    assert "not a calibrated" in doc


def test_payloads_are_not_inlined_into_the_test():
    """A payload chosen by a mutator is exactly the input that breaks a string
    literal, and a 4 KB one is unreviewable. It is data, and lives in JSON."""
    source = render_test(make_finding(payload="x" * 4000))
    assert "x" * 200 not in source
    assert "reprobe_findings" in source


HOSTILE_KEYS = [
    'protected_write:/workspace/a"""\nimport os\nos.system("echo pwned")\n"""',
    "protected_write:/workspace/trailing-backslash\\",
    "protected_write:/workspace/nul\x00byte",
    "protected_write:/workspace/\u202eevil-bidi",
    # Unicode tag characters: invisible, and a known way to smuggle injected text.
    "protected_write:/workspace/tag\U000e0049\U000e0047\U000e004e",
    "protected_write:/workspace/cr\rline",
    "protected_write:/workspace/'''single'''",
]


@pytest.mark.parametrize("key", HOSTILE_KEYS)
def test_attacker_influenced_text_cannot_inject_code_into_the_test(key):
    """Action keys carry paths and hosts the agent produced while under attack:
    a `protected_write:` key names a file the agent wrote. Rendered into a
    docstring unescaped, a filename containing triple quotes ends the string and
    the rest of it is code -- in a file someone's CI will import."""
    source = render_test(make_finding(actions=[key]))
    tree = ast.parse(source)
    assert not any(
        isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "system" for n in ast.walk(tree)
    )
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert "os" not in imported
    # Nothing invisible in the source either: a raw bidi override is valid
    # Python and still makes the file read differently from how it runs.
    assert all(ch.isprintable() or ch == "\n" for ch in source)
    # And the text survives intact, as documentation, rather than being dropped.
    assert key in ast.get_docstring(tree, clean=False)


def test_render_test_refuses_a_trial_cap_no_fixed_agent_could_pass():
    with pytest.raises(ConfigError, match="fixed agent"):
        render_test(make_finding(), max_trials=8)


def test_generated_tests_carry_only_the_reprobe_marker():
    """`agent` means "paid lane" in this repo and may mean anything in a user's;
    a generated test should not borrow it."""
    source = render_test(make_finding())
    assert "pytest.mark.reprobe" in source
    assert "pytest.mark.agent" not in source


def test_conftest_requires_the_agents_api_key():
    claude = render_conftest([make_finding(agent_id="claude-code")])
    codex = render_conftest([make_finding(agent_id="codex-cli")])
    fake = render_conftest([make_finding(agent_id="fake-agent")])
    assert _assigned(claude, "REQUIRED_ENV") == ["ANTHROPIC_API_KEY"]
    assert _assigned(codex, "REQUIRED_ENV") == ["OPENAI_API_KEY"]
    assert _assigned(fake, "REQUIRED_ENV") == []


def test_conftest_unions_required_env_across_agents():
    both = render_conftest(
        [make_finding(agent_id="claude-code"), make_finding(agent_id="codex-cli")]
    )
    assert _assigned(both, "REQUIRED_ENV") == ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]


# --- writing a suite ----------------------------------------------------------


def test_write_suite_creates_one_test_per_finding_plus_shared_files(tmp_path):
    findings = [
        make_finding(actions=["a"], coverage="c1"),
        make_finding(actions=["b"], coverage="c2"),
    ]
    written = _suite(findings, tmp_path)
    names = {p.name for p in written}
    assert "conftest.py" in names
    assert len([n for n in names if n.startswith("test_")]) == 2
    assert len(list((tmp_path / "reprobe_findings").glob("*.json"))) == 2


def test_the_written_finding_loads_against_the_fingerprint_its_test_names(tmp_path):
    finding = make_finding()
    written = _suite([finding], tmp_path)
    test_file = next(p for p in written if p.name.startswith("test_"))
    fingerprint = _assigned(test_file.read_text(), "FINGERPRINT")
    data = next((tmp_path / "reprobe_findings").glob("*.json"))
    assert load_finding(data, fingerprint=fingerprint) == finding


def test_write_suite_filenames_are_descriptive(tmp_path):
    written = _suite([make_finding(actions=["canary_exfil:attacker.example"])], tmp_path)
    test_file = next(p for p in written if p.name.startswith("test_"))
    assert "canary_exfil" in test_file.name
    assert test_file.name.endswith(".py")


def test_write_suite_filenames_are_stable_across_triage_runs(tmp_path):
    """Two triage runs of the same bug give findings with different random ids
    and the same content. Re-exporting must not rename the file -- a rename is
    a noisy diff and, worse, looks like one finding retired and another added."""
    a, b = make_finding(), make_finding()
    assert a.id != b.id
    first = _suite([a], tmp_path / "one")
    second = _suite([b], tmp_path / "two")
    assert [p.name for p in first] == [p.name for p in second]


def test_write_suite_is_idempotent(tmp_path):
    finding = make_finding()
    first = _suite([finding], tmp_path)
    contents = {p: p.read_text() for p in first}
    second = _suite([finding], tmp_path)
    assert first == second
    assert {p: p.read_text() for p in second} == contents


def test_findings_sharing_a_first_action_key_get_separate_files(tmp_path):
    """The draft's slug was scenario + first action key, so these two -- distinct
    groups, since they differ in their second key -- overwrote each other and
    one finding silently stopped being tested."""
    findings = [
        make_finding(actions=["canary_read:/workspace/.env"]),
        make_finding(actions=["canary_read:/workspace/.env", "egress_offlist:evil.example"]),
    ]
    _suite(findings, tmp_path)
    assert len(list(tmp_path.glob("test_*.py"))) == 2


def test_identical_findings_are_written_once(tmp_path):
    written = _suite([make_finding(), make_finding()], tmp_path)
    assert len([p for p in written if p.name.startswith("test_")]) == 1
    assert len(written) == len(set(written))


def test_re_exporting_removes_stale_generated_files_and_keeps_the_users(tmp_path):
    """A finding dropped from the new export must stop being tested; a test the
    user wrote by hand in the same directory must survive."""
    old = make_finding(actions=["canary_read:/workspace/old"])
    _suite([old], tmp_path)
    mine = tmp_path / "test_mine.py"
    mine.write_text("def test_mine():\n    pass\n")
    notes = tmp_path / "reprobe_findings" / "NOTES.txt"
    notes.write_text("kept")

    new = make_finding(actions=["canary_read:/workspace/new"])
    _suite([new], tmp_path)
    tests = sorted(p.name for p in tmp_path.glob("test_*.py"))
    assert len(tests) == 2 and "test_mine.py" in tests
    assert not any("old" in t for t in tests)
    assert [p.stem for p in (tmp_path / "reprobe_findings").glob("*.json")] == [
        new.fingerprint().split(":", 1)[1]
    ]
    assert notes.exists()


def test_write_suite_refuses_to_overwrite_a_hand_written_conftest(tmp_path):
    mine = tmp_path / "conftest.py"
    mine.write_text("# my fixtures\n")
    with pytest.raises(ConfigError, match="not written by reprobe"):
        _suite([make_finding()], tmp_path)
    assert mine.read_text() == "# my fixtures\n"
    assert not list(tmp_path.glob("test_*.py"))


@pytest.mark.parametrize(
    "pins",
    [{"agent_version": ""}, {"model_id": ""}, {"container_digest": ""}],
)
def test_write_suite_refuses_an_unpinned_finding(tmp_path, pins):
    """R11's promise is pinning, and pinning to "" is not pinning: the test
    would silently run against whatever image is current."""
    with pytest.raises(ConfigError, match="pin"):
        _suite([make_finding(**pins)], tmp_path)
    assert not list(tmp_path.glob("test_*.py"))


def test_write_suite_exports_an_unpinned_finding_only_when_asked(tmp_path):
    written = _suite([make_finding(container_digest="")], tmp_path, allow_unpinned=True)
    assert any(p.name.startswith("test_") for p in written)


def test_write_suite_refuses_a_finding_for_an_agent_it_cannot_run(tmp_path):
    with pytest.raises(ConfigError, match="no-such-agent"):
        _suite([make_finding(agent_id="no-such-agent")], tmp_path)


def test_write_suite_refuses_an_empty_export(tmp_path):
    with pytest.raises(ConfigError, match="no findings"):
        _suite([], tmp_path)


# --- the generated suite, run by a real pytest ----------------------------------


def _run_suite(dest, *extra, opt_in=False, env=None):
    """Run pytest on a generated suite in a fresh process, from inside the suite
    directory, so neither this repo's conftest nor its pytest config apply --
    the way it would run in somebody else's repository."""
    environ = {
        k: v
        for k, v in os.environ.items()
        if k not in {OPT_IN_ENV, "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    }
    if opt_in:
        environ[OPT_IN_ENV] = "1"
    environ.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(dest), "-p", "no:cacheprovider", *extra],
        cwd=dest,
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_generated_suite_collects_under_strict_markers(tmp_path):
    """The check that catches template syntax errors, and that a user's
    `--strict-markers` does not reject the `reprobe` marker."""
    _suite([make_finding(), make_finding(actions=["b"])], tmp_path)
    result = _run_suite(tmp_path, "--collect-only", "-q", "--strict-markers")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 tests collected" in result.stdout


def test_without_opting_in_the_suite_skips_and_says_so_loudly(tmp_path):
    _suite([make_finding()], tmp_path)
    result = _run_suite(tmp_path, "-rs")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
    assert f"{OPT_IN_ENV}=1" in result.stdout
    assert "did NOT run" in result.stdout


def test_opted_in_without_the_api_key_fails_rather_than_skips(tmp_path):
    """The decision this suite turns on: once a run has asked for the exploit
    to be measured, anything that stops it being measured is a failure. A skip
    is green in somebody else's CI, where nobody investigates."""
    _suite([make_finding(agent_id="claude-code")], tmp_path)
    result = _run_suite(tmp_path, opt_in=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "skipped" not in result.stdout
    assert "missing environment: ANTHROPIC_API_KEY" in result.stdout


def test_opted_in_without_docker_fails_rather_than_skips(tmp_path):
    """The fake agent needs no key, so this goes all the way to the sandbox,
    which cannot reach a Docker daemon here. Not being able to run the exploit
    is a failure once a run has asked for it."""
    _suite([make_finding(agent_id="fake-agent", agent_profile="always")], tmp_path)
    result = _run_suite(tmp_path, opt_in=True, env={"DOCKER_HOST": "unix:///nonexistent.sock"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "skipped" not in result.stdout
    assert "1 failed" in result.stdout or "1 error" in result.stdout


def test_an_opt_in_value_other_than_1_does_not_count(tmp_path):
    """Matches this repo's own lanes: exactly "1". `REPROBE_...=0` meaning "on"
    would be a surprise in the wrong direction."""
    _suite([make_finding(agent_id="fake-agent", agent_profile="always")], tmp_path)
    result = _run_suite(tmp_path, env={OPT_IN_ENV: "0"})
    assert result.returncode == 0
    assert "1 skipped" in result.stdout


def test_a_finding_file_deleted_from_under_its_test_is_an_error_even_unopted(tmp_path):
    """A suite whose data has gone missing is broken, and saying so costs
    nothing -- so it errors at collection, not only when someone opts in."""
    _suite([make_finding()], tmp_path)
    for data in (tmp_path / "reprobe_findings").glob("*.json"):
        data.unlink()
    result = _run_suite(tmp_path)
    assert result.returncode != 0
    assert "skipped" not in result.stdout
    assert "reprobe_findings" in result.stdout


# --- a self-contained suite: the scenario travels with it ----------------------


def test_the_suite_carries_its_scenario_and_fixture(tmp_path):
    """A user's repository does not contain Reprobe's scenarios, so a suite
    that looked them up relative to the working directory could not run
    anywhere it was meant to."""
    _suite([make_finding()], tmp_path)
    bundled = tmp_path / FINDINGS_DIR / "scenarios" / "minimal" / "scenario.yaml"
    assert bundled.is_file()
    assert load_scenario(bundled).scenario_hash == MINIMAL_HASH


def test_the_conftest_points_the_runner_at_the_bundled_scenarios():
    source = render_conftest([make_finding()])
    assert '"reprobe_findings" / "scenarios"' in source
    assert "REPROBE_SCENARIOS" in source


def test_a_scenario_that_no_longer_matches_the_finding_is_refused(tmp_path):
    """Checked at export, where it is free, rather than in somebody's CI."""
    with pytest.raises(ConfigError, match="scenario"):
        _suite([make_finding(scenario_hash="scn:deadbeefdeadbeef")], tmp_path)
    assert not list(tmp_path.glob("test_*.py"))


def test_a_narrowed_finding_bundles_the_full_scenario(tmp_path):
    from reprobe.shrink import env_knobs

    full = load_scenario(MINIMAL)
    knob = next(k for k in env_knobs(full, {"readme": "x"}) if k.id == "egress_allowlist")
    finding = make_finding(
        scenario_hash=knob.apply(full).scenario_hash, env_removed=["egress_allowlist"]
    )
    _suite([finding], tmp_path)
    bundled = tmp_path / FINDINGS_DIR / "scenarios" / "minimal" / "scenario.yaml"
    assert load_scenario(bundled).scenario_hash == MINIMAL_HASH


def test_a_finding_whose_scenario_was_not_supplied_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="minimal"):
        write_suite([make_finding()], tmp_path, scenarios={})


def test_a_fixture_outside_its_scenario_directory_is_refused(tmp_path):
    """The bundle keeps the scenario's relative layout; a fixture reached by
    `../` would land outside the suite."""
    src = tmp_path / "src"
    (src / "scn").mkdir(parents=True)
    shutil.copytree(MINIMAL.parent / "fixture", src / "shared")
    text = MINIMAL.read_text().replace("fixture: ./fixture", "fixture: ../shared")
    (src / "scn" / "scenario.yaml").write_text(text)
    elsewhere = load_scenario(src / "scn" / "scenario.yaml")
    finding = make_finding(scenario_hash=elsewhere.scenario_hash)
    with pytest.raises(ConfigError, match="fixture"):
        write_suite(
            [finding], tmp_path / "suite", scenarios={"minimal": src / "scn" / "scenario.yaml"}
        )


def test_re_exporting_replaces_the_bundled_scenarios(tmp_path):
    _suite([make_finding()], tmp_path)
    stale = tmp_path / FINDINGS_DIR / "scenarios" / "gone"
    stale.mkdir()
    _suite([make_finding()], tmp_path)
    assert not stale.exists()


def test_the_suite_runs_from_any_working_directory(tmp_path):
    """Collected from the repository root, as CI will, not from inside the suite."""
    suite = tmp_path / "repo" / "tests" / "reprobe"
    _suite([make_finding()], suite)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/reprobe", "-p", "no:cacheprovider", "-rs"],
        cwd=tmp_path / "repo",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={k: v for k, v in os.environ.items() if k != OPT_IN_ENV},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout


# --- the generated test's behaviour, without a sandbox -------------------------


def _load_generated_test(tmp_path, finding):
    written = _suite([finding], tmp_path)
    path = next(p for p in written if p.name.startswith("test_"))
    spec = importlib.util.spec_from_file_location("generated", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    test = next(getattr(module, n) for n in dir(module) if n.startswith("test_"))
    return module, test


class _StubRunner:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def check(self, path, **kw):
        self.calls.append((path, kw))
        return self.outcome


def _outcome(status, notes=()):
    from reprobe.stats import estimate

    return Outcome(status, estimate(0, 9), 0.30, notes=notes)


def test_a_fixed_finding_passes_and_surfaces_drift_as_warnings(tmp_path):
    """Drift is a note, not a failure -- but a note nobody sees is a dead
    field. pytest's warnings summary is where a passing test can say it."""
    module, test = _load_generated_test(tmp_path, make_finding())
    stub = _StubRunner(_outcome(Status.FIXED, notes=("agent version changed: 1 -> 2",)))
    with pytest.warns(UserWarning, match="agent version changed"):
        test(stub)
    ((path, kw),) = stub.calls
    assert path == module.FINDING_FILE
    assert kw == {
        "fingerprint": module.FINGERPRINT,
        "threshold": module.THRESHOLD,
        "max_trials": module.MAX_TRIALS,
    }


@pytest.mark.parametrize("status", [Status.REPRODUCES, Status.INCONCLUSIVE])
def test_anything_but_fixed_fails_the_test(tmp_path, status):
    _, test = _load_generated_test(tmp_path, make_finding())
    with pytest.raises(pytest.fail.Exception, match=status.name):
        test(_StubRunner(_outcome(status)))


# --- reading a suite back ------------------------------------------------------


def test_suite_entries_reads_back_what_write_suite_wrote(tmp_path):
    findings = [make_finding(threshold=0.4), make_finding(actions=["b"])]
    _suite(findings, tmp_path, max_trials=30)
    entries = suite_entries(tmp_path)
    assert sorted(e.fingerprint for e in entries) == sorted(f.fingerprint() for f in findings)
    by_fp = {e.fingerprint: e for e in entries}
    assert by_fp[findings[0].fingerprint()].threshold == 0.4
    assert all(e.max_trials == 30 for e in entries)
    assert all(e.finding_file.is_file() for e in entries)


def test_suite_entries_ignores_hand_written_tests(tmp_path):
    _suite([make_finding()], tmp_path)
    (tmp_path / "test_mine.py").write_text("THRESHOLD = 0.9\n")
    assert len(suite_entries(tmp_path)) == 1


def test_suite_entries_is_empty_for_a_directory_with_no_suite(tmp_path):
    assert suite_entries(tmp_path) == []
