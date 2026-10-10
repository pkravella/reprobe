import json

import pytest

from reprobe.finding import Finding
from tests.support.findings import make_finding


def test_record_round_trip_is_lossless():
    original = make_finding()
    restored = Finding.from_record(json.loads(json.dumps(original.to_record())))
    assert restored == original


def test_title_reads_like_something_a_human_would_scan():
    finding = make_finding(actions=["canary_exfil:attacker.example"], successes=12, trials=20)
    title = finding.title()
    assert "canary_exfil" in title
    assert "attacker.example" in title
    assert "%" in title


def test_title_still_reads_when_there_is_no_action_key():
    assert make_finding(actions=[]).title().startswith("violation")


def test_fingerprint_is_stable_and_content_addressed():
    a = make_finding()
    b = make_finding()
    assert a.fingerprint() == b.fingerprint()
    assert make_finding(payload="other").fingerprint() != a.fingerprint()


def test_finding_carries_everything_r11_needs_to_pin_a_test():
    finding = make_finding()
    for field in (
        "scenario_hash",
        "agent_meta",
        "sandbox_description",
        "threshold",
        "reprobe_version",
    ):
        assert getattr(finding, field) is not None


def test_shrunk_bytes_defaults_to_the_size_of_the_payload_it_reports():
    finding = make_finding(payload="abcde")
    assert finding.shrunk_bytes == 5
    assert finding.reduction == 1.0 - 5 / 4000


def test_shrunk_bytes_cannot_disagree_with_the_payload():
    """`reduction` is the Phase-3 gate's headline number and it is computed from
    `shrunk_bytes`. A finding whose byte count does not match its own payload
    reports a reduction for a payload nobody has -- the same class of bug as the
    shrinker reporting a rate for a candidate it did not keep."""
    with pytest.raises(ValueError, match="shrunk_bytes"):
        make_finding(payload="abcde", shrunk_bytes=9999)


def test_the_interval_is_recomputed_from_the_counts_on_load():
    """The counts are the data; the interval is derived from them. A stored
    record claiming a lower bound its counts do not support is corrected rather
    than believed -- which is the safe direction, and means a hand-edited
    findings file cannot talk a finding over the threshold."""
    record = make_finding(successes=2, trials=20).to_record()
    record["rate"]["lo"] = 0.95
    restored = Finding.from_record(record)
    assert restored.rate.lo < 0.30
    assert restored.rate.successes == 2


def test_a_finding_records_which_prerequisites_came_out():
    finding = make_finding(env_removed=["egress_allowlist"])
    assert finding.env_removed == ["egress_allowlist"]


def test_reduction_of_a_finding_with_no_original_size_is_zero_not_a_crash():
    """`original_bytes` is 0 when a finding was never shrunk -- a candidate
    exported straight from the search, say. Reporting 0% reduction is honest;
    dividing by zero is not."""
    finding = make_finding(original_bytes=0)
    assert finding.reduction == 0.0


def test_env_removed_summary_reads_either_way():
    assert "egress" in make_finding(env_removed=["egress_allowlist"]).env_removed_summary()
    assert make_finding().env_removed_summary() == "every declared prerequisite is load-bearing"
