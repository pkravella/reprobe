"""The shipped scenario pack.

These are the scenarios a user gets on install and the ones every gate and
benchmark number is measured on, so the tests here are mostly about the pack
being *honest*: real work for the agent, no real credentials, and nothing that
fires a check before an attacker has done anything.
"""

import re
from pathlib import Path

import pytest

from reprobe.scenario import load_scenario

PACK = sorted(Path("scenarios").glob("*/scenario.yaml"))
IDS = [p.parent.name for p in PACK]


def test_the_pack_has_ten_scenarios():
    assert len(PACK) == 10


@pytest.mark.parametrize("path", PACK, ids=IDS)
def test_every_scenario_loads(path):
    load_scenario(path)


@pytest.mark.parametrize("path", PACK, ids=IDS)
def test_scenario_names_match_their_directories(path):
    assert load_scenario(path).name == path.parent.name


@pytest.mark.parametrize("path", PACK, ids=IDS)
def test_every_scenario_gives_the_agent_real_work(path):
    scenario = load_scenario(path)
    assert len(scenario.task.split()) >= 8, "the task must be plausible honest work"
    files = [p for p in scenario.fixture_dir.rglob("*") if p.is_file()]
    assert len(files) >= 3, "a one-file repo is not a GitHub-style repository task"


@pytest.mark.parametrize("path", PACK, ids=IDS)
def test_every_scenario_declares_checks_and_a_canary_or_protected_path(path):
    scenario = load_scenario(path)
    assert scenario.checks
    assert scenario.canaries or scenario.protected_paths


@pytest.mark.parametrize("path", PACK, ids=IDS)
def test_no_fixture_contains_a_real_looking_credential(path):
    bad = re.compile(
        r"sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    )
    scenario = load_scenario(path)
    for file in scenario.fixture_dir.rglob("*"):
        if file.is_file():
            assert not bad.search(file.read_text(errors="ignore")), file


@pytest.mark.parametrize("path", PACK, ids=IDS)
def test_no_fixture_file_is_itself_the_attack(path):
    """The payload goes in the surface at trial time; the fixture is honest.

    A fixture that ships the injection text means every trial starts already
    compromised, and the shrinker would reduce to the empty payload.
    """
    scenario = load_scenario(path)
    tells = re.compile(r"ignore (all )?previous instructions|RPRB_CANARY_", re.I)
    for file in scenario.fixture_dir.rglob("*"):
        if file.is_file():
            assert not tells.search(file.read_text(errors="ignore")), file


def test_the_pack_covers_every_check_type():
    types = {c.type for path in PACK for c in load_scenario(path).checks}
    assert types == {
        "canary_read",
        "canary_exfil",
        "protected_write",
        "egress_offlist",
        "dangerous_command",
    }


def test_the_pack_covers_distinct_attack_surfaces():
    """Ten scenarios that all inject through README.md would be one scenario."""
    paths = {s.path for p in PACK for s in load_scenario(p).surfaces}
    assert len(paths) >= 8, paths


@pytest.mark.parametrize("path", PACK, ids=IDS)
def test_every_surface_is_a_kind_the_harness_actually_delivers(path):
    """`http`, `tool_output` and `terminal` surfaces are declared in the schema
    but nothing renders them yet -- `materialise` skips them and no other code
    picks them up. A scenario using one would have its payload silently
    dropped and every trial would come back clean.
    """
    for surface in load_scenario(path).surfaces:
        assert surface.kind == "file", f"{surface.id} uses unwired kind {surface.kind!r}"


@pytest.mark.parametrize("path", PACK, ids=IDS)
def test_the_free_lane_can_reach_every_surface(path):
    """Each scenario must be runnable on the fake agent, or it cannot take part
    in a gate: both arms would score zero and the comparison would read as a
    tie rather than as a scenario that never delivered its payload."""
    from reprobe.sandbox.docker_sandbox import _surface_paths

    scenario = load_scenario(path)

    class _Spec:
        pass

    spec = _Spec()
    spec.scenario = scenario
    assert _surface_paths(spec), f"{scenario.name} declares no file surface"
