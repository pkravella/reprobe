import os
import random
import subprocess
from pathlib import Path

import pytest

from reprobe.agents.base import AgentSpec
from reprobe.canary import mint
from reprobe.errors import HarnessError, ScenarioError
from reprobe.ids import digest_bytes
from reprobe.sandbox import TrialSpec
from reprobe.sandbox.workspace import WorkspaceManifest, materialise
from reprobe.scenario import CanarySpec, Surface, load_scenario

MINIMAL = Path("tests/data/scenarios/minimal/scenario.yaml")


def _spec(payload: str = "PWN") -> TrialSpec:
    scn = load_scenario(MINIMAL)
    rng = random.Random(0)
    return TrialSpec(
        trial_id="trial_t",
        scenario=scn,
        agent=AgentSpec(id="claude-code", model="claude-haiku-4-5"),
        payloads={"readme": payload},
        canaries=[mint(c, rng) for c in scn.canaries],
        seed=0,
    )


def _with_canaries(spec: TrialSpec, *specs: CanarySpec) -> TrialSpec:
    rng = random.Random(1)
    scenario = spec.scenario.model_copy(update={"canaries": list(specs)})
    return spec.model_copy(update={"scenario": scenario, "canaries": [mint(s, rng) for s in specs]})


def test_fixture_files_are_copied(tmp_path):
    materialise(_spec(), tmp_path)
    assert (tmp_path / "src" / "widget.js").exists()


def test_surface_template_is_rendered_with_the_payload(tmp_path):
    materialise(_spec("INJECT-ME"), tmp_path)
    readme = (tmp_path / "README.md").read_text()
    assert "INJECT-ME" in readme
    assert "# Widget" in readme
    assert "{{ payload }}" not in readme


def test_the_surface_overwrites_the_fixtures_own_file(tmp_path):
    # README.md exists in the fixture AND is a surface. The surface must win,
    # or the payload is never placed and every trial is a silent false negative.
    fixture_readme = (load_scenario(MINIMAL).fixture_dir / "README.md").read_text()
    materialise(_spec("INJECT-ME"), tmp_path)
    rendered = (tmp_path / "README.md").read_text()
    assert rendered != fixture_readme
    assert "INJECT-ME" in rendered


def test_canary_file_is_written_with_the_canary_value(tmp_path):
    spec = _spec()
    materialise(spec, tmp_path)
    assert spec.canaries[0].value in (tmp_path / ".env").read_text()


def test_canary_file_is_keyed_by_the_canary_id(tmp_path):
    # The plan hardcoded `API_KEY=`, which happens to match the minimal
    # scenario's canary id -- so asserting on that id proves nothing. Use a
    # different id, and one with a hyphen, which no dotenv parser accepts as a
    # key.
    spec = _with_canaries(_spec(), CanarySpec(id="db-password", kind="file", path=".env"))
    materialise(spec, tmp_path)
    content = (tmp_path / ".env").read_text()
    assert content.startswith("DB_PASSWORD=")
    assert spec.canaries[0].value in content


def test_workspace_is_a_git_repo_with_one_commit(tmp_path):
    materialise(_spec(), tmp_path)
    log = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(log.strip().splitlines()) == 1


def test_the_workspace_starts_from_a_clean_tree(tmp_path):
    # Agents routinely run `git status` to orient themselves. A workspace that
    # starts dirty is both unrealistic and a confound: changes the agent did
    # not make would show up in its own view of the repo. Whether the canary is
    # tracked or ignored is the fixture author's call -- either way the tree
    # must be clean at hand-off.
    materialise(_spec(), tmp_path)
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == "", f"workspace starts dirty:\n{status}"


def test_manifest_captures_every_file(tmp_path):
    materialise(_spec(), tmp_path)
    manifest = WorkspaceManifest.of(tmp_path)
    assert "README.md" in manifest.files
    assert ".env" in manifest.files
    assert not any(p.startswith(".git/") for p in manifest.files)


def test_diff_reports_created_modified_and_deleted(tmp_path):
    materialise(_spec(), tmp_path)
    before = WorkspaceManifest.of(tmp_path)
    (tmp_path / "NEW.md").write_text("new")
    (tmp_path / "README.md").write_text("changed")
    (tmp_path / "src" / "widget.js").unlink()
    diff = before.diff(WorkspaceManifest.of(tmp_path))
    assert diff.created == ["NEW.md"]
    assert diff.modified == ["README.md"]
    assert diff.deleted == ["src/widget.js"]


def test_diff_of_an_untouched_workspace_is_empty(tmp_path):
    materialise(_spec(), tmp_path)
    before = WorkspaceManifest.of(tmp_path)
    diff = before.diff(WorkspaceManifest.of(tmp_path))
    assert (diff.created, diff.modified, diff.deleted) == ([], [], [])
    assert diff.touched == []


def test_materialise_refuses_a_payload_for_an_unknown_surface(tmp_path):
    spec = _spec()
    bad = spec.model_copy(update={"payloads": {"ghost": "x"}})
    with pytest.raises(ScenarioError, match="ghost"):
        materialise(bad, tmp_path)


def test_an_invalid_spec_is_refused_before_anything_is_written(tmp_path):
    # The plan copied the fixture first and validated afterwards, leaving a
    # half-built workspace behind on the error path.
    bad = _spec().model_copy(update={"payloads": {"ghost": "x"}})
    with pytest.raises(ScenarioError):
        materialise(bad, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_surface_with_no_payload_renders_an_empty_slot(tmp_path):
    spec = _spec().model_copy(update={"payloads": {}})
    materialise(spec, tmp_path)
    assert "{{ payload }}" not in (tmp_path / "README.md").read_text()


# --- canaries that cannot be planted ---------------------------------------


def test_a_file_canary_without_a_path_is_refused_not_skipped(tmp_path):
    # The plan's `if kind == "file" and spec.path:` silently skipped it. An
    # unplanted canary makes canary_read and canary_exfil unfirable, so every
    # trial reports clean: a false negative, the worst outcome for this tool.
    spec = _with_canaries(_spec(), CanarySpec(id="orphan", kind="file", path=None))
    with pytest.raises(ScenarioError, match="orphan"):
        materialise(spec, tmp_path)


def test_an_env_canary_writes_no_file_and_is_not_an_error(tmp_path):
    # env canaries reach the container as environment variables (Task 13), so
    # materialise deliberately plants nothing for them.
    spec = _with_canaries(_spec(), CanarySpec(id="token", kind="env", env_var="SECRET_TOKEN"))
    materialise(spec, tmp_path)
    assert not (tmp_path / "token").exists()
    assert not (tmp_path / "SECRET_TOKEN").exists()


# --- symlinks: the fsdiff blind spot ---------------------------------------


def test_the_manifest_records_a_symlink(tmp_path):
    # The plan skipped every symlink. fsdiff is the authoritative source for
    # protected_write ("a diff cannot miss a write"), and symlinking a
    # protected path is a standard evasion, so a symlink must be visible.
    materialise(_spec(), tmp_path)
    before = WorkspaceManifest.of(tmp_path)
    os.symlink("/etc/hosts", tmp_path / "sneaky")
    diff = before.diff(WorkspaceManifest.of(tmp_path))
    assert diff.created == ["sneaky"]


def test_a_symlink_is_recorded_by_target_not_by_dereferenced_content(tmp_path):
    # Hashing through the link would read host files into the manifest and make
    # a symlink indistinguishable from a copy.
    materialise(_spec(), tmp_path)
    (tmp_path / "real.txt").write_text("same bytes")
    os.symlink("real.txt", tmp_path / "link.txt")
    files = WorkspaceManifest.of(tmp_path).files
    assert files["link.txt"] != files["real.txt"]
    assert files["link.txt"] != digest_bytes(b"same bytes")


def test_a_broken_symlink_is_still_recorded(tmp_path):
    materialise(_spec(), tmp_path)
    before = WorkspaceManifest.of(tmp_path)
    os.symlink("nowhere-at-all", tmp_path / "dangling")
    assert before.diff(WorkspaceManifest.of(tmp_path)).created == ["dangling"]


def test_retargeting_a_symlink_reads_as_a_modification(tmp_path):
    materialise(_spec(), tmp_path)
    os.symlink("/etc/hosts", tmp_path / "shifty")
    before = WorkspaceManifest.of(tmp_path)
    (tmp_path / "shifty").unlink()
    os.symlink("/etc/passwd", tmp_path / "shifty")
    assert before.diff(WorkspaceManifest.of(tmp_path)).modified == ["shifty"]


def test_the_manifest_does_not_follow_symlinked_directories(tmp_path):
    # Verified on CPython 3.11: rglob does not traverse a directory symlink, so
    # a loop cannot hang the walk and a link to / cannot pull the host in.
    materialise(_spec(), tmp_path)
    os.symlink(tmp_path, tmp_path / "loop")
    os.symlink("/etc", tmp_path / "escape")
    files = WorkspaceManifest.of(tmp_path).files
    assert "loop" in files
    assert "escape" in files
    assert not any(p.startswith(("loop/", "escape/")) for p in files)


# --- harness failures ------------------------------------------------------


def test_a_missing_git_is_a_harness_error(monkeypatch, tmp_path):
    # Not a ScenarioError and not a violation: the harness could not run, so
    # the trial has no verdict.
    import reprobe.sandbox.workspace as ws

    monkeypatch.setattr(ws.shutil, "which", lambda _name: None)
    with pytest.raises(HarnessError, match="git"):
        materialise(_spec(), tmp_path)


def test_a_failing_git_reports_its_stderr(monkeypatch, tmp_path):
    import reprobe.sandbox.workspace as ws

    def _boom(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: bad things")

    monkeypatch.setattr(ws.subprocess, "run", _boom)
    with pytest.raises(HarnessError, match="fatal: bad things"):
        materialise(_spec(), tmp_path)


def test_a_non_file_surface_is_not_written_to_disk(tmp_path):
    # http / tool_output / terminal surfaces are served by the gateway, so
    # materialise must leave no file behind for them.
    spec = _spec()
    surfaces = [
        *spec.scenario.surfaces,
        Surface(id="api", path="unused", kind="http", template="x"),
    ]
    scenario = spec.scenario.model_copy(update={"surfaces": surfaces})
    materialise(spec.model_copy(update={"scenario": scenario}), tmp_path)
    assert not (tmp_path / "unused").exists()
