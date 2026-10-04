from pathlib import Path

import pytest

from reprobe.errors import ScenarioError
from reprobe.scenario import load_scenario

MINIMAL = Path("tests/data/scenarios/minimal/scenario.yaml")


def _write(tmp_path: Path, body: str, *, fixture: bool = True) -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(body)
    if fixture:
        (tmp_path / "fixture").mkdir(exist_ok=True)
        (tmp_path / "fixture" / "README.md").write_text("x")
    return path


# ------------------------------------------------------------------- happy path


def test_loads_minimal_scenario():
    scn = load_scenario(MINIMAL)
    assert scn.name == "minimal"
    assert scn.surface("readme").kind == "file"
    assert "{{ payload }}" in scn.surface("readme").template
    assert scn.limits.max_trial_seconds == 180


def test_defaults_apply_for_omitted_limits(tmp_path):
    path = _write(
        tmp_path,
        "name: d\nversion: 1\ntask: t\nfixture: ./fixture\n"
        "surfaces:\n  - {id: r, path: R.md, kind: file, template: '{{ payload }}'}\n"
        "checks: []\n",
    )
    limits = load_scenario(path).limits
    assert (limits.max_trial_seconds, limits.max_turns) == (300, 40)


def test_fixture_dir_is_resolved_relative_to_the_yaml_file():
    scn = load_scenario(MINIMAL)
    assert scn.fixture_dir.is_absolute()
    assert (scn.fixture_dir / "README.md").exists()


def test_surface_lookup_raises_for_an_unknown_id():
    with pytest.raises(ScenarioError, match="ghost"):
        load_scenario(MINIMAL).surface("ghost")


def test_scenario_is_immutable():
    scn = load_scenario(MINIMAL)
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        scn.name = "other"


# ----------------------------------------------------------------------- hashing


def test_scenario_hash_is_stable_and_prefixed():
    a = load_scenario(MINIMAL)
    b = load_scenario(MINIMAL)
    assert a.scenario_hash == b.scenario_hash
    assert a.scenario_hash.startswith("scn:")


def test_scenario_hash_changes_when_task_changes(tmp_path):
    text = MINIMAL.read_text().replace("Read README.md", "Read the README file")
    path = _write(tmp_path, text)
    assert load_scenario(path).scenario_hash != load_scenario(MINIMAL).scenario_hash


def test_scenario_hash_ignores_where_the_fixture_lives_on_disk(tmp_path):
    """Moving a scenario must not invalidate findings exported from it."""
    import shutil

    moved = tmp_path / "elsewhere"
    shutil.copytree(MINIMAL.parent, moved)
    assert load_scenario(moved / "scenario.yaml").scenario_hash == (
        load_scenario(MINIMAL).scenario_hash
    )


def test_scenario_hash_changes_when_fixture_content_changes(tmp_path):
    """Editing a fixture file changes the agent's environment, so it must change
    the hash even though the YAML is byte-identical."""
    import shutil

    edited = tmp_path / "edited"
    shutil.copytree(MINIMAL.parent, edited)
    (edited / "fixture" / "src" / "widget.js").write_text("// different\n")
    assert load_scenario(edited / "scenario.yaml").scenario_hash != (
        load_scenario(MINIMAL).scenario_hash
    )


def test_scenario_hash_changes_when_a_fixture_file_is_added(tmp_path):
    import shutil

    extra = tmp_path / "extra"
    shutil.copytree(MINIMAL.parent, extra)
    (extra / "fixture" / "EXTRA.md").write_text("new file\n")
    assert load_scenario(extra / "scenario.yaml").scenario_hash != (
        load_scenario(MINIMAL).scenario_hash
    )


def test_model_copy_with_an_update_recomputes_the_hash():
    """`model_copy` is how the environment shrinker narrows a scenario. A cached
    hash that survives the copy would record a finding against a scenario it was
    never tested on."""
    scn = load_scenario(MINIMAL)
    original = scn.scenario_hash
    narrowed = scn.model_copy(update={"egress_allowlist": []})
    assert narrowed.scenario_hash != original


def test_model_copy_without_an_update_keeps_the_same_hash():
    scn = load_scenario(MINIMAL)
    assert scn.model_copy().scenario_hash == scn.scenario_hash


# ---------------------------------------------------------------- validation


def test_surface_without_payload_placeholder_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./fixture\n"
        "surfaces:\n  - {id: r, path: README.md, kind: file, template: 'no slot'}\n"
        "checks: []\n",
    )
    with pytest.raises(ScenarioError, match="payload"):
        load_scenario(path)


def test_missing_fixture_dir_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./nope\n"
        "surfaces:\n  - {id: r, path: R.md, kind: file, template: '{{ payload }}'}\n"
        "checks: []\n",
        fixture=False,
    )
    with pytest.raises(ScenarioError, match="fixture"):
        load_scenario(path)


def test_check_referencing_unknown_canary_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./fixture\n"
        "surfaces:\n  - {id: r, path: R.md, kind: file, template: '{{ payload }}'}\n"
        "canaries: []\n"
        "checks:\n  - {type: canary_read, params: {canary: ghost}}\n",
    )
    with pytest.raises(ScenarioError, match="ghost"):
        load_scenario(path)


def test_duplicate_surface_ids_are_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./fixture\n"
        "surfaces:\n"
        "  - {id: r, path: A.md, kind: file, template: '{{ payload }}'}\n"
        "  - {id: r, path: B.md, kind: file, template: '{{ payload }}'}\n"
        "checks: []\n",
    )
    with pytest.raises(ScenarioError, match="duplicate"):
        load_scenario(path)


def test_duplicate_canary_ids_are_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./fixture\n"
        "surfaces:\n  - {id: r, path: R.md, kind: file, template: '{{ payload }}'}\n"
        "canaries:\n"
        "  - {id: k, kind: file, path: .env}\n"
        "  - {id: k, kind: file, path: .secret}\n"
        "checks: []\n",
    )
    with pytest.raises(ScenarioError, match="duplicate"):
        load_scenario(path)


def test_scenario_without_surfaces_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./fixture\nsurfaces: []\nchecks: []\n",
    )
    with pytest.raises(ScenarioError, match="surface"):
        load_scenario(path)


def test_unknown_check_type_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./fixture\n"
        "surfaces:\n  - {id: r, path: R.md, kind: file, template: '{{ payload }}'}\n"
        "checks:\n  - {type: vibes_based, params: {}}\n",
    )
    with pytest.raises(ScenarioError):
        load_scenario(path)


def test_unknown_surface_kind_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./fixture\n"
        "surfaces:\n  - {id: r, path: R.md, kind: telepathy, template: '{{ payload }}'}\n"
        "checks: []\n",
    )
    with pytest.raises(ScenarioError):
        load_scenario(path)


def test_file_canary_without_a_path_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./fixture\n"
        "surfaces:\n  - {id: r, path: R.md, kind: file, template: '{{ payload }}'}\n"
        "canaries:\n  - {id: k, kind: file}\n"
        "checks: []\n",
    )
    with pytest.raises(ScenarioError, match="path"):
        load_scenario(path)


def test_env_canary_without_a_var_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        "name: bad\nversion: 1\ntask: t\nfixture: ./fixture\n"
        "surfaces:\n  - {id: r, path: R.md, kind: file, template: '{{ payload }}'}\n"
        "canaries:\n  - {id: k, kind: env}\n"
        "checks: []\n",
    )
    with pytest.raises(ScenarioError, match="env_var"):
        load_scenario(path)


def test_malformed_yaml_is_rejected_with_the_path_named(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text("name: [unclosed\n")
    with pytest.raises(ScenarioError, match=str(path.name)):
        load_scenario(path)


def test_non_mapping_yaml_is_rejected(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ScenarioError, match="mapping"):
        load_scenario(path)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(ScenarioError, match="cannot read"):
        load_scenario(tmp_path / "absent.yaml")
