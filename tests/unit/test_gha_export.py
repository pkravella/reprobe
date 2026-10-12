import pytest
import yaml

import reprobe.export.gha as gha
from reprobe.errors import ConfigError
from reprobe.export.gha import render_workflow, write_workflow
from tests.support.findings import make_finding

REF = "v0.1.0"


def _doc(findings=None, **kw):
    kw.setdefault("reprobe_ref", REF)
    return yaml.safe_load(render_workflow(findings or [make_finding()], **kw))


def _job(doc):
    return doc["jobs"]["reprobe"]


def _runs(doc):
    return [str(step.get("run", "")) for step in _job(doc)["steps"]]


def test_workflow_is_valid_yaml_with_the_expected_shape():
    doc = _doc()
    assert doc["name"] == "reprobe"
    assert set(doc) == {"name", True, "permissions", "jobs"}  # PyYAML reads `on:` as True


def test_workflow_runs_on_a_schedule_on_demand_and_on_suite_changes():
    doc = _doc(schedule="0 6 * * 1", suite_dir="tests/reprobe")
    triggers = doc[True]
    assert triggers["schedule"] == [{"cron": "0 6 * * 1"}]
    assert "workflow_dispatch" in triggers
    assert triggers["pull_request"]["paths"] == ["tests/reprobe/**"]


def test_the_job_only_runs_for_pull_requests_from_this_repository():
    """Fork PRs get no secrets, so an opted-in suite would fail on every one of
    them by design. Same-repo PRs still re-check an edited suite."""
    condition = _job(_doc())["if"]
    assert "github.event_name != 'pull_request'" in condition
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition


def test_the_workflow_opts_the_suite_in():
    """Without this every test skips and the job is green having measured nothing."""
    assert _job(_doc())["env"]["REPROBE_REGRESSION_TESTS"] == "1"


def test_the_token_is_read_only():
    assert _doc()["permissions"] == {"contents": "read"}


def _suite_step(doc):
    return next(s for s in _job(doc)["steps"] if "pytest" in str(s.get("run", "")))


def test_only_the_secrets_the_findings_agents_need_are_passed():
    claude = _suite_step(_doc([make_finding(agent_id="claude-code")]))["env"]
    assert claude["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    assert "OPENAI_API_KEY" not in claude
    fake = _suite_step(_doc([make_finding(agent_id="fake-agent", agent_profile="always")]))
    assert "env" not in fake


def test_no_credential_literal_ever_appears():
    text = render_workflow([make_finding()], reprobe_ref=REF)
    assert "sk-" not in text


def test_the_workflow_sets_a_budget_cap():
    assert _job(_doc(max_usd=12.5))["env"]["REPROBE_MAX_USD"] == "12.5"


def test_reprobe_is_checked_out_at_the_pinned_ref():
    """The user's repository has neither Reprobe's Makefile nor its images, and
    the images are built from the same source the suite is run with."""
    steps = _job(_doc())["steps"]
    source = next(s for s in steps if s.get("with", {}).get("repository") == gha.REPROBE_REPOSITORY)
    assert source["with"]["ref"] == REF
    assert source["with"]["path"] == gha.SOURCE_DIR


def test_only_the_images_the_findings_need_are_built():
    runs = _runs(_doc([make_finding(agent_id="fake-agent", agent_profile="always")]))
    build = next(r for r in runs if "make" in r)
    assert build.split()[-3:] == ["base", "mockgw", "fakeagent"]
    assert "claude-code" not in build


def test_images_for_several_agents_are_built_once_each():
    findings = [
        make_finding(agent_id="claude-code"),
        make_finding(agent_id="codex-cli"),
        make_finding(agent_id="claude-code", payload="another"),
    ]
    build = next(r for r in _runs(_doc(findings)) if "make" in r)
    assert build.split()[-4:] == ["base", "mockgw", "claude-code", "codex-cli"]


def test_nothing_assumes_the_users_repo_is_reprobe():
    """The draft ran `uv sync --dev` and `make images` in the user's checkout."""
    runs = " ".join(_runs(_doc()))
    assert "make images" not in runs
    assert "uv sync" not in runs
    assert f"--with ./{gha.SOURCE_DIR}" in runs


def test_the_suite_path_reaches_the_shell_as_a_variable_not_as_text():
    """A path interpolated into `run:` is shell code; via env it is data."""
    doc = _doc(suite_dir="tests/my suite;rm -rf ~")
    assert _job(doc)["env"]["REPROBE_SUITE"] == "tests/my suite;rm -rf ~"
    assert all("rm -rf" not in r for r in _runs(doc))
    assert any('"./$REPROBE_SUITE"' in r for r in _runs(doc))


def test_a_development_version_must_name_its_ref(monkeypatch):
    """There is no tag for a dev build, and `main` would not be a pin."""
    monkeypatch.setattr(gha, "__version__", "0.1.0.dev0")
    with pytest.raises(ConfigError, match="reprobe_ref"):
        render_workflow([make_finding()])


def test_a_release_defaults_to_its_own_tag(monkeypatch):
    monkeypatch.setattr(gha, "__version__", "0.2.0")
    doc = yaml.safe_load(render_workflow([make_finding()]))
    source = next(s for s in _job(doc)["steps"] if "repository" in s.get("with", {}))
    assert source["with"]["ref"] == "v0.2.0"


@pytest.mark.parametrize("ref", ["", "main; curl x", "${{ github.token }}", "a b", "-x"])
def test_a_ref_that_is_not_a_plain_git_ref_is_refused(ref):
    with pytest.raises(ConfigError, match="ref"):
        render_workflow([make_finding()], reprobe_ref=ref)


@pytest.mark.parametrize("schedule", ["", "* * *", "0 6 * * 1 extra", "0 6 * * ${{x}}"])
def test_a_schedule_that_is_not_five_cron_fields_is_refused(schedule):
    with pytest.raises(ConfigError, match="schedule"):
        render_workflow([make_finding()], reprobe_ref=REF, schedule=schedule)


def test_an_actions_expression_in_the_suite_path_is_refused():
    """`${{ }}` inside any value is evaluated by Actions before the shell sees
    it, env or not."""
    with pytest.raises(ConfigError, match="suite"):
        render_workflow([make_finding()], reprobe_ref=REF, suite_dir="tests/${{ github.token }}")


def test_workflow_documents_which_findings_it_guards():
    finding = make_finding(actions=["canary_exfil:attacker.example"])
    assert "canary_exfil:attacker.example" in render_workflow([finding], reprobe_ref=REF)


@pytest.mark.parametrize(
    "key",
    [
        "protected_write:/w/x\nname: pwned",
        "protected_write:/w/x\rjobs: {}",
        "protected_write:/w/x\u2028on: push",
        "protected_write:/w/x\x85permissions: write-all",
    ],
)
def test_attacker_influenced_text_in_the_header_cannot_change_the_workflow(key):
    """The header comment names the findings' action keys, which carry paths
    the agent produced under attack. A line break ends a YAML comment -- and
    PyYAML, like YAML 1.1, also breaks on NEL and the Unicode separators."""
    text = render_workflow([make_finding(actions=[key])], reprobe_ref=REF)
    # Parsing alone cannot see an injected key: PyYAML lets the later, real
    # `name:` and `jobs:` overwrite it. So: every line of the header is still a
    # comment. `splitlines` breaks on NEL and U+2028/9 too, as YAML 1.1 does.
    lines = text.splitlines()
    header = lines[: lines.index("name: reprobe")]
    assert all(line.startswith("#") or not line.strip() for line in header), header
    doc = yaml.safe_load(text)
    assert doc["name"] == "reprobe"
    assert set(doc) == {"name", True, "permissions", "jobs"}
    assert doc["permissions"] == {"contents": "read"}


def test_an_empty_export_is_refused():
    with pytest.raises(ConfigError, match="no findings"):
        render_workflow([], reprobe_ref=REF)


def test_write_workflow_creates_its_directory(tmp_path):
    dest = tmp_path / ".github" / "workflows" / "reprobe.yml"
    path = write_workflow([make_finding()], dest, reprobe_ref=REF)
    assert path == dest
    assert yaml.safe_load(dest.read_text())["name"] == "reprobe"


def test_a_finding_for_an_unknown_agent_is_a_clean_refusal():
    """The image list was computed before the agent was checked, so this was a
    raw KeyError and a traceback."""
    with pytest.raises(ConfigError, match="nope"):
        render_workflow([make_finding(agent_id="nope")], reprobe_ref=REF)


def test_secrets_reach_only_the_step_that_runs_the_suite():
    """Not the job: the image build and the package build run code from a
    checkout of a mutable tag, and need no API key."""
    job = _job(_doc([make_finding(agent_id="claude-code")]))
    assert not any("secrets." in str(v) for v in job["env"].values())
    run = next(s for s in job["steps"] if "pytest" in str(s.get("run", "")))
    assert run["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    build = next(s for s in job["steps"] if "make" in str(s.get("run", "")))
    assert "env" not in build


def test_a_suite_path_starting_with_a_dash_is_not_an_option():
    """pytest has no `--`; a leading `./` keeps the path a path."""
    run = next(r for r in _runs(_doc()) if "pytest" in r)
    assert '"./$REPROBE_SUITE"' in run
