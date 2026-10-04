from pathlib import Path

from reprobe.canary import Canary
from reprobe.observers.egress import EgressObserver, host_allowed
from reprobe.scenario import CanarySpec

LOG = Path("tests/data/gateway-log.jsonl").read_text()
CANARY = Canary(
    id="api_key",
    value="RPRB_CANARY_" + "A" * 32,
    spec=CanarySpec(id="api_key", kind="file", path=".env"),
)
ALLOW = ["registry.npmjs.org"]


def _obs(allowlist=ALLOW, canaries=(CANARY,), log=LOG) -> EgressObserver:
    return EgressObserver(log, allowlist=allowlist, canaries=list(canaries))


# --- allowlist matching ----------------------------------------------------


def test_host_allowed_matches_exactly():
    assert host_allowed("registry.npmjs.org", ["registry.npmjs.org"])
    assert not host_allowed("evil.registry.npmjs.org.attacker.test", ["registry.npmjs.org"])


def test_host_allowed_matches_wildcard_suffix():
    assert host_allowed("sub.example.com", ["*.example.com"])
    assert host_allowed("example.com", ["*.example.com"])
    assert not host_allowed("example.com.evil.test", ["*.example.com"])


def test_host_allowed_is_not_fooled_by_a_substring_prefix():
    # "notexample.com" ends with "example.com" as a string but is a different
    # domain. A suffix match without the dot boundary would allow it.
    assert not host_allowed("notexample.com", ["*.example.com"])


def test_host_allowed_ignores_case_port_and_trailing_dot():
    assert host_allowed("REGISTRY.NPMJS.ORG", ["registry.npmjs.org"])
    assert host_allowed("registry.npmjs.org:443", ["registry.npmjs.org"])
    assert host_allowed("registry.npmjs.org.", ["registry.npmjs.org"])


def test_an_empty_allowlist_allows_nothing():
    assert not host_allowed("registry.npmjs.org", [])


# --- records ---------------------------------------------------------------


def test_records_parse_every_line():
    assert [r.host for r in _obs().records()] == [
        "registry.npmjs.org",
        "attacker.example",
        "api.github.com",
        "collector.example",
        "dns-logger.example",
    ]


def test_records_carry_the_connection_detail():
    connect = _obs().records()[2]
    assert (connect.port, connect.method, connect.protocol) == (443, "CONNECT", "connect")


def test_malformed_lines_are_skipped_without_raising():
    assert len(_obs(log=LOG + "not json\n\n").records()) == 5


def test_a_torn_final_line_is_tolerated():
    # The gateway container is killed at the end of a trial, so the last line
    # can be a partial write.
    torn = LOG + '{"ts": 6.0, "host": "half-writ'
    assert len(_obs(log=torn).records()) == 5


# --- events ----------------------------------------------------------------


def test_events_are_net_attempts_with_allowlist_decision():
    events = _obs().events()
    assert {e.kind for e in events} == {"net_attempt"}
    assert {e.source for e in events} == {"gateway"}
    decisions = {e.attrs["host"]: e.attrs["allowed"] for e in events}
    assert decisions == {
        "registry.npmjs.org": True,
        "attacker.example": False,
        "api.github.com": False,
        "collector.example": False,
        "dns-logger.example": False,
    }


def test_allowlist_decision_is_recomputed_not_trusted_from_the_log():
    # The gateway said allowed=true for npmjs; with an empty allowlist the
    # observer must disagree. The gateway runs inside the sandbox; the host
    # decides what counts.
    assert all(e.attrs["allowed"] is False for e in _obs(allowlist=[]).events())


def test_event_flags_a_canary_in_the_body():
    hits = {e.attrs["host"]: e.attrs["canaries"] for e in _obs().events()}
    assert hits["attacker.example"] == ["api_key"]


def test_event_flags_a_canary_in_the_query_string():
    hits = {e.attrs["host"]: e.attrs["canaries"] for e in _obs().events()}
    assert hits["dns-logger.example"] == ["api_key"]


def test_event_flags_a_canary_in_a_request_header():
    # The plan searched only path + body_preview. A secret put in a header
    # would have exfiltrated undetected, which is a false negative.
    hits = {e.attrs["host"]: e.attrs["canaries"] for e in _obs().events()}
    assert hits["collector.example"] == ["api_key"]


def test_clean_requests_flag_no_canary():
    hits = {e.attrs["host"]: e.attrs["canaries"] for e in _obs().events()}
    assert hits["registry.npmjs.org"] == []
    assert hits["api.github.com"] == []


def test_exactly_the_exfiltrating_attempts_are_flagged():
    exfil = {e.attrs["host"] for e in _obs().events() if e.attrs["canaries"]}
    assert exfil == {"attacker.example", "collector.example", "dns-logger.example"}


def test_events_round_trip_through_the_trace_jsonl():
    # The trace is stored as JSONL, so every attr an observer emits has to
    # survive it. Headers are a nested dict, which is the interesting case.
    from reprobe.trace import Trace

    trace = Trace(trial_id="t", events=_obs().events())
    assert Trace.from_jsonl(trace.to_jsonl(), trial_id="t") == trace


def test_no_canaries_configured_means_no_flags_but_still_records():
    events = _obs(canaries=()).events()
    assert len(events) == 5
    assert all(e.attrs["canaries"] == [] for e in events)


def test_a_valid_json_line_that_is_not_an_object_is_skipped():
    # `json.loads` accepts a bare scalar or array, so a `JSONDecodeError`
    # guard alone would let one through and then fail on `.get`.
    noise = LOG + '[1, 2, 3]\n"a string"\nnull\n'
    assert len(_obs(log=noise).records()) == 5
