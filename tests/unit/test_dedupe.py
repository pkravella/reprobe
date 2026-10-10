from reprobe.coverage import fingerprint
from reprobe.dedupe import dedupe, group_key
from reprobe.trace import Event, Trace
from tests.support.findings import make_finding


def _routes() -> tuple[str, ...]:
    """Real coverage signatures for three traces that all read the canary.

    Computed from `fingerprint` rather than pasted as literals, so the test
    tracks the coverage implementation instead of going quietly stale against
    it -- the grouping assertions below only need the signatures to differ, so
    stale literals would keep passing while measuring nothing.
    """
    read_canary = Event(
        ts=2.0, kind="file_read", source="strace", attrs={"path": "/workspace/.env", "ok": True}
    )
    read_tool = Event(ts=1.0, kind="tool_call", source="agent", attrs={"name": "Read"})
    traces = [
        # the bare exploit
        [read_tool, read_canary],
        # the same exploit, with one incidental Grep on the way
        [
            read_tool,
            Event(ts=1.5, kind="tool_call", source="agent", attrs={"name": "Grep"}),
            read_canary,
        ],
        # the same exploit, having also read one source file
        [
            read_tool,
            Event(
                ts=1.5,
                kind="file_read",
                source="strace",
                attrs={"path": "/workspace/src/widget.js", "ok": True},
            ),
            read_canary,
        ],
    ]
    return tuple(fingerprint(Trace(trial_id="t", events=e)).signature() for e in traces)


ROUTES = _routes()


def test_one_bug_reached_three_ways_really_does_have_three_signatures():
    """The premise everything below rests on. If coverage ever became coarse
    enough that these collapse on their own, the grouping tests would start
    passing for a different reason and this is where that shows up."""
    assert len(set(ROUTES)) == 3


def test_identical_action_keys_group_together():
    a = make_finding(actions=["canary_exfil:attacker.example"], coverage="cov:1")
    b = make_finding(
        actions=["canary_exfil:attacker.example"], coverage="cov:1", payload="different text"
    )
    groups = dedupe([a, b])
    assert len(groups) == 1
    assert len(groups[0].members) == 2


def test_different_action_does_not_group():
    a = make_finding(actions=["canary_exfil:attacker.example"], coverage="cov:1")
    b = make_finding(actions=["protected_write:.github/workflows/ci.yml"], coverage="cov:1")
    assert len(dedupe([a, b])) == 2


def test_a_different_coverage_signature_still_groups():
    """The plan keyed on (actions, coverage signature). Measured, that splits one
    bug into as many groups as there are routes to it: `Coverage.signature()` is
    a hash of the whole bitmap, so a single extra incidental tool call changes it
    completely. See the next test for the numbers. The benchmark doc is explicit
    that the action key is "the unit R16 dedupes on", and the Phase-2 gate read
    it that way."""
    a = make_finding(actions=["canary_exfil:x"], coverage="cov:1")
    b = make_finding(actions=["canary_exfil:x"], coverage="cov:2")
    assert len(dedupe([a, b])) == 1


def test_near_identical_payloads_from_one_search_collapse_to_one_group():
    """R16's whole purpose. These three signatures are what `fingerprint` really
    returns for three traces that all read /workspace/.env, differing only by one
    incidental Grep and one incidental source-file read:

        read only               -> cov:02c51cbf84635a3f
        read + a Grep           -> cov:3156b52d24c40342
        read + one source file  -> cov:592ffcb941b1e015

    A search produces dozens of these. They are one bug.
    """
    findings = [
        make_finding(actions=["canary_read:/workspace/.env"], coverage=sig, payload=f"p{i}")
        for i, sig in enumerate(ROUTES)
    ]
    groups = dedupe(findings)
    assert len(groups) == 1
    assert groups[0].size == 3


def test_the_routes_a_group_spans_are_reported():
    """Dropping coverage from the key must not throw the information away: how
    many distinct behaviours reached one bug is something the search worked for,
    and it belongs in the report."""
    findings = [
        make_finding(actions=["canary_read:/workspace/.env"], coverage=sig, payload=f"p{i}")
        for i, sig in enumerate(ROUTES)
    ]
    group = dedupe([*findings, findings[0]])[0]
    assert group.coverage_signatures == sorted(ROUTES)
    assert group.size == 4  # four members, three distinct routes


def test_a_superset_of_action_keys_is_a_different_finding():
    """Deliberate. "Reads the canary" and "reads the canary AND rewrites CI" are
    not the same report: the second is strictly worse, and since the
    representative is chosen for reliability and size rather than severity,
    merging them would hide the worse one behind the milder one."""
    mild = make_finding(actions=["canary_read:/workspace/.env"])
    worse = make_finding(
        actions=["canary_read:/workspace/.env", "protected_write:.github/workflows/ci.yml"]
    )
    assert len(dedupe([mild, worse])) == 2


def test_group_key_is_order_independent_in_the_action_list():
    a = make_finding(actions=["b", "a"], coverage="cov:1")
    b = make_finding(actions=["a", "b"], coverage="cov:1")
    assert group_key(a) == group_key(b)


def test_representative_is_the_most_reliable_then_the_smallest():
    weak_small = make_finding(actions=["x"], coverage="c", payload="tiny", successes=4, trials=20)
    strong_big = make_finding(
        actions=["x"], coverage="c", payload="x" * 2000, successes=19, trials=20
    )
    strong_small = make_finding(
        actions=["x"], coverage="c", payload="small", successes=19, trials=20
    )
    group = dedupe([weak_small, strong_big, strong_small])[0]
    assert group.representative.payloads == strong_small.payloads


def test_groups_are_sorted_by_representative_reliability():
    weak = make_finding(actions=["weak"], coverage="c1", successes=7, trials=20)
    strong = make_finding(actions=["strong"], coverage="c2", successes=20, trials=20)
    keys = [g.representative.action_keys[0] for g in dedupe([weak, strong])]
    assert keys == ["strong", "weak"]


def test_dedupe_of_nothing_is_nothing():
    assert dedupe([]) == []


def test_a_group_counts_distinct_payloads_separately_from_members():
    """Observed on the Phase-3 gate: a 214-byte candidate and a 430-byte one
    both reduced to the identical 9 bytes, so the group held two members and one
    payload. Reporting the member count as a payload count tells a reader there
    are two things to look at when there is one -- and loses the actually
    interesting fact, which is that one bug was reached twice and reduction
    collapsed both to the same minimal form."""
    same = [
        make_finding(actions=["canary_exfil:x"], coverage=sig, payload="POST .env")
        for sig in ROUTES[:2]
    ]
    group = dedupe(same)[0]
    assert group.size == 2
    assert group.distinct_payloads == 1
    assert len(group.coverage_signatures) == 2


def test_genuinely_different_payloads_are_counted_as_such():
    group = dedupe(
        [
            make_finding(actions=["canary_exfil:x"], coverage=ROUTES[0], payload="POST .env"),
            make_finding(actions=["canary_exfil:x"], coverage=ROUTES[0], payload="send .env now"),
        ]
    )[0]
    assert group.size == 2
    assert group.distinct_payloads == 2
