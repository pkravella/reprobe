import pytest

from reprobe.agents.base import AgentMeta
from reprobe.sandbox import FsDiff, TrialResult
from reprobe.sandbox.fake import FakeSandbox
from reprobe.trace import Event, Trace


def _result(trial_id="t", violated_path=None, harness_error=None):
    events = [Event(ts=1.0, kind="tool_call", source="agent", attrs={"name": "Read"})]
    if violated_path:
        events.append(
            Event(ts=2.0, kind="file_read", source="strace", attrs={"path": violated_path})
        )
    return TrialResult(
        trial_id=trial_id,
        exit_code=0,
        trace=Trace(trial_id=trial_id, events=events),
        fs_diff=FsDiff(),
        agent_meta=AgentMeta(agent_id="fake", agent_version="0", model_id="m", prompt_hash="p"),
        harness_error=harness_error,
    )


def test_returns_the_configured_result(minimal_spec):
    out = FakeSandbox(lambda spec: _result()).run(minimal_spec)
    assert out.exit_code == 0


def test_the_trial_id_is_made_authoritative(minimal_spec):
    # The callable may hand back a result built with its own id; the caller
    # correlates by the spec's id, so the sandbox rewrites it -- on the trace
    # too, or a stored trace points at the wrong trial.
    out = FakeSandbox(lambda spec: _result(trial_id="something-else")).run(minimal_spec)
    assert out.trial_id == minimal_spec.trial_id
    assert out.trace.trial_id == minimal_spec.trial_id


def test_records_every_call(minimal_spec):
    sandbox = FakeSandbox(lambda spec: _result())
    sandbox.run(minimal_spec)
    sandbox.run(minimal_spec)
    assert len(sandbox.calls) == 2
    assert sandbox.calls[0] is minimal_spec


def test_the_callable_sees_the_payloads_so_a_test_can_model_a_real_agent(minimal_spec):
    def behaviour(spec):
        leaked = "LEAK" in spec.payloads.get("readme", "")
        return _result(violated_path="/workspace/.env" if leaked else None)

    sandbox = FakeSandbox(behaviour)
    clean = sandbox.run(minimal_spec)
    dirty = sandbox.run(minimal_spec.model_copy(update={"payloads": {"readme": "LEAK"}}))
    assert not clean.trace.of_kind("file_read")
    assert dirty.trace.of_kind("file_read")


def test_a_callable_can_model_a_flaky_agent(minimal_spec):
    # The reason the fake sandbox exists: the rate estimator and shrinker need
    # a behaviour that reproduces some of the time, keyed on the trial seed.
    def flaky(spec):
        import random

        fires = random.Random(spec.seed).random() < 0.6
        return _result(violated_path="/workspace/.env" if fires else None)

    sandbox = FakeSandbox(flaky)
    outcomes = [
        bool(sandbox.run(minimal_spec.model_copy(update={"seed": s})).trace.of_kind("file_read"))
        for s in range(40)
    ]
    assert 0 < sum(outcomes) < 40, "a flaky behaviour must be neither always nor never"


def test_dict_form_falls_back_to_the_wildcard(minimal_spec):
    assert FakeSandbox({"*": _result()}).run(minimal_spec).exit_code == 0


def test_dict_form_keys_on_the_payload_digest(minimal_spec):
    from reprobe.ids import digest

    keyed = {digest(minimal_spec.payloads): _result()}
    assert FakeSandbox(keyed).run(minimal_spec).exit_code == 0


def test_dict_form_raises_when_no_result_matches(minimal_spec):
    with pytest.raises(KeyError):
        FakeSandbox({}).run(minimal_spec)


def test_a_harness_error_result_passes_through(minimal_spec):
    out = FakeSandbox(lambda spec: _result(harness_error="boom")).run(minimal_spec)
    assert out.ok is False
    assert out.harness_error == "boom"


def test_describe_identifies_itself_as_fake():
    assert FakeSandbox({}).describe()["runtime"] == "fake"


def test_it_satisfies_the_sandbox_protocol(minimal_spec):
    from reprobe.sandbox import SandboxProtocol

    sandbox: SandboxProtocol = FakeSandbox(lambda spec: _result())
    assert sandbox.run(minimal_spec).ok
    assert "runtime" in sandbox.describe()
