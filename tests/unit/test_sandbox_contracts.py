from reprobe.agents.base import AgentMeta
from reprobe.sandbox import EgressRecord, FsDiff, TrialResult
from reprobe.trace import Trace


def _result(**overrides) -> TrialResult:
    base = dict(
        trial_id="trial_1",
        exit_code=0,
        trace=Trace(trial_id="trial_1"),
        fs_diff=FsDiff(),
        agent_meta=AgentMeta(
            agent_id="claude-code",
            agent_version="2.1.289",
            model_id="claude-haiku-4-5",
            prompt_hash="abc",
        ),
    )
    return TrialResult(**{**base, **overrides})


def test_a_trial_without_a_harness_error_is_usable():
    assert _result().ok is True


def test_a_harness_error_means_no_verdict_not_a_clean_pass():
    # Load-bearing throughout triage: a harness failure must never be counted
    # as a trial that ran clean, or the search records a false negative and the
    # Phase-1 gate reports zero harness failures while having had many.
    result = _result(harness_error="docker daemon unreachable")
    assert result.ok is False
    assert result.exit_code == 0, "a harness error can coexist with exit code 0"


def test_touched_merges_and_sorts_every_kind_of_change():
    diff = FsDiff(created=["b", "a"], modified=["a"], deleted=["c"])
    assert diff.touched == ["a", "b", "c"]


def test_an_egress_record_defaults_to_denied():
    # The gateway must declare a connection allowed; defaulting to allowed
    # would turn a parsing gap into a silently permitted exfiltration.
    record = EgressRecord(ts=1.0, host="evil.test", port=443)
    assert record.allowed is False
