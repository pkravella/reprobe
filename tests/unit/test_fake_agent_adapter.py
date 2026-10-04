"""The scripted fake agent's host-side adapter (Task 34).

The point of this agent is that it is *not* a mock: it is a real process in the
real container making real syscalls, with a seeded probability model instead of
a model. That is what makes every integration test and every phase gate free,
and it gives three things a real agent cannot — CI on every PR, a
vendor-independent regression suite for the engine, and **ground truth**, since
the true reproduction rate and the true minimal trigger are configured rather
than guessed at.

These are the host-side tests. The container's behaviour is exercised in
`tests/integration/test_fakeagent.py`.
"""

import json

import pytest

from reprobe import __version__
from reprobe.agents import available, get_adapter
from reprobe.agents.base import AgentSpec
from reprobe.agents.fake_agent import ACTIONS, PROFILES, FakeAgentAdapter


def _spec(**kw) -> AgentSpec:
    return AgentSpec(**{"id": "fake-agent", "model": "reprobe-fake", **kw})


# --- registry -------------------------------------------------------------


def test_the_registry_exposes_the_fake_agent():
    assert isinstance(get_adapter("fake-agent"), FakeAgentAdapter)
    assert "fake-agent" in available()


def test_identity_and_image():
    adapter = FakeAgentAdapter()
    assert adapter.id == "fake-agent"
    assert adapter.image == "reprobe/fakeagent:dev"


def test_it_costs_nothing():
    # The whole reason this exists. `reprobe-fake` is in the price table at
    # zero so the ledger's arithmetic stays honest instead of special-casing.
    from reprobe.budget import price

    assert price("reprobe-fake", 10_000, 10_000, 10_000) == 0.0


# --- the command line -----------------------------------------------------


def test_command_passes_the_task():
    cmd = FakeAgentAdapter().command(_spec(), task="do the thing", max_usd=0.25)
    assert cmd[0] == "reprobe-fake-agent"
    assert "do the thing" in cmd


def test_the_profile_travels_in_the_environment_not_on_the_command_line():
    # The plan put `--profile <name>` in extra_args and had the container
    # declare a `--profile` argparse flag -- which it then never read, taking
    # the profile from REPROBE_FAKE_PROFILE instead. So that test asserted on a
    # dead flag and proved nothing.
    #
    # The environment is also the only mechanism that works: a profile is
    # nested JSON, not a name the container could look up, because the host
    # owns the profile table.
    cmd = FakeAgentAdapter().command(_spec(), task="t", max_usd=0.25)
    assert not any("profile" in part for part in cmd)
    assert "REPROBE_FAKE_PROFILE" in FakeAgentAdapter.profile_env("flaky_60", seed=1)


def test_extra_args_are_appended():
    cmd = FakeAgentAdapter().command(_spec(extra_args=["--quiet"]), task="t", max_usd=1.0)
    assert cmd[-1] == "--quiet"


def test_no_host_environment_is_inherited():
    # Its configuration arrives through TrialSpec.env_overrides, so there is
    # nothing to copy from the host. Declaring REPROBE_FAKE_* here would let a
    # developer's stray shell export reach a trial.
    assert FakeAgentAdapter().env_allowlist == ()


def test_it_needs_no_login_and_caps_no_spend():
    adapter = FakeAgentAdapter()
    assert adapter.login_command() is None
    assert adapter.enforces_max_usd is False


# --- the profile table ----------------------------------------------------


def test_the_named_profiles_the_gates_depend_on_all_exist():
    assert set(PROFILES) >= {
        "never",
        "always",
        "flaky_60",
        "placement_sensitive",
        "conjunction",
    }


def test_named_profiles_cover_every_check():
    actions = {t["action"] for p in PROFILES.values() for t in p["triggers"]}
    assert actions >= {"read_canary", "exfil_canary", "write_protected", "dangerous_command"}


def test_every_profile_action_is_one_the_container_implements():
    # A typo in a profile would otherwise make a gate silently never fire.
    actions = {t["action"] for p in PROFILES.values() for t in p["triggers"]}
    assert actions <= set(ACTIONS), f"unknown actions: {actions - set(ACTIONS)}"


def test_profiles_are_json_serialisable():
    # They cross into the container as an environment variable.
    for name, profile in PROFILES.items():
        assert json.loads(json.dumps(profile)) == profile, name


def test_never_fires_nothing_and_always_fires_certainly():
    assert PROFILES["never"]["triggers"] == []
    always = PROFILES["always"]["triggers"][0]
    assert always["probability"] == 1.0


def test_flaky_60_is_genuinely_between_the_extremes():
    # The workhorse for the triage tests: a real finding that does not
    # reproduce every time, so the Wilson interval and the shrinker have
    # something honest to be measured against.
    probability = PROFILES["flaky_60"]["triggers"][0]["probability"]
    assert 0.0 < probability < 1.0


def test_placement_sensitive_has_a_narrow_attention_window():
    # The gradient the search has to climb: a payload must be worded right AND
    # placed early, so a scheduler that keeps a half-successful parent beats
    # one that re-rolls from scratch.
    narrow = PROFILES["placement_sensitive"]["triggers"][0]["window"]
    wide = PROFILES["flaky_60"]["triggers"][0]["window"]
    assert narrow < wide


def test_conjunction_needs_two_needles_at_once():
    for trigger in PROFILES["conjunction"]["triggers"]:
        assert len(trigger["all_of"]) >= 2


def test_every_profile_does_the_honest_task_too():
    # A trial where the agent only attacks and never does the work is not a
    # trial, it is a smoke test. Coverage and the checks both need the honest
    # baseline present.
    assert all(p["honest_task"] for p in PROFILES.values())


# --- profile_env ----------------------------------------------------------


def test_profile_env_carries_the_profile_and_the_seed():
    env = FakeAgentAdapter.profile_env("flaky_60", seed=7)
    assert json.loads(env["REPROBE_FAKE_PROFILE"]) == PROFILES["flaky_60"]
    assert env["REPROBE_FAKE_SEED"] == "7"


def test_profile_env_values_are_all_strings():
    # They become container environment variables.
    env = FakeAgentAdapter.profile_env("always", seed=0)
    assert all(isinstance(v, str) for v in env.values())


def test_an_unknown_profile_name_is_refused_with_the_known_ones_listed():
    with pytest.raises(KeyError, match="flaky_60"):
        FakeAgentAdapter.profile_env("typo", seed=0)


def test_different_seeds_produce_different_environments():
    # The seed must be per-trial, not per-run: with one seed every trial of a
    # flaky profile gives the same answer and the rate estimator is untested.
    assert FakeAgentAdapter.profile_env("flaky_60", 1) != FakeAgentAdapter.profile_env(
        "flaky_60", 2
    )


# --- stream parsing -------------------------------------------------------


def test_parse_stdout_reads_the_same_stream_format_as_claude_code():
    # The container emits Claude Code's shape on purpose, and the adapter
    # reuses that parser rather than duplicating it -- two parsers would drift,
    # and the drift would weaken exactly what the integration test proves.
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"Read","input":{"file_path":"/workspace/README.md"}}]}}\n'
        '{"type":"result","subtype":"success","is_error":false,'
        '"usage":{"input_tokens":10,"output_tokens":5}}\n'
    )
    events, cost = FakeAgentAdapter().parse_stdout(stream, model="reprobe-fake")
    assert [e.attrs["name"] for e in events if e.kind == "tool_call"] == ["Read"]
    assert cost.usd == 0.0


def test_parsing_defaults_to_the_free_model_so_cost_stays_zero():
    stream = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"usage":{"input_tokens":1000,"output_tokens":1000}}\n'
    )
    _, cost = FakeAgentAdapter().parse_stdout(stream)
    assert cost.total_tokens == 2000
    assert cost.usd == 0.0


def test_a_tool_result_reaches_the_text_channel():
    # The container emits tool_results carrying real file content, so a canary
    # it reads is visible to a check that scans agent text -- which is the only
    # evidence when the agent reads without writing anything.
    stream = (
        '{"type":"user","message":{"content":[{"type":"tool_result",'
        '"tool_use_id":"t1","content":"API_KEY=RPRB_CANARY_X"}]}}\n'
    )
    events, _ = FakeAgentAdapter().parse_stdout(stream)
    assert any("RPRB_CANARY_X" in e.attrs.get("text", "") for e in events)


def test_an_error_in_the_stream_is_surfaced():
    stream = (
        '{"type":"result","subtype":"success","is_error":true,'
        '"usage":{"input_tokens":0,"output_tokens":0},"result":"boom"}\n'
    )
    assert FakeAgentAdapter().error_from(stream) == "boom"


def test_a_clean_stream_reports_no_error():
    stream = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"usage":{"input_tokens":1,"output_tokens":1}}\n'
    )
    assert FakeAgentAdapter().error_from(stream) is None


def test_version_is_the_reprobe_version():
    # Not a vendor's version: the thing under test here is Reprobe itself, so
    # that is what a finding from this lane is pinned to.
    assert FakeAgentAdapter().version_from("anything") == __version__
