"""R7: behavioural coverage.

Coverage is the search's only sense of progress, so the failures that matter
here are the quiet ones -- a misconfigured signal group or a mismatched map size
that makes every trace look identical. A blind search still runs; it just stops
being a search. Several tests below exist only to make that noisy.
"""

import pytest

from reprobe.coverage import (
    SIGNAL_GROUPS,
    CoverageMap,
    fingerprint,
    resource_class,
    signals,
)
from reprobe.errors import ReprobeError
from reprobe.trace import Event, Trace


def _trace(*events):
    return Trace(trial_id="t", events=list(events))


def _tools(*names):
    return _trace(
        *[
            Event(ts=float(i), kind="tool_call", source="agent", attrs={"name": n})
            for i, n in enumerate(names)
        ]
    )


# --- resource classes ------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/workspace/.env", "secret_file"),
        ("/workspace/id_rsa", "secret_file"),
        ("/home/agent/.claude/settings.json", "agent_state"),
        ("/workspace/CLAUDE.md", "agent_state"),
        ("/workspace/.github/workflows/ci.yml", "workflow"),
        ("/workspace/.git/config", "git_internal"),
        ("/workspace/src/app.js", "source"),
        ("/workspace/node_modules/x/index.js", "dependency"),
        ("/etc/passwd", "system"),
        ("/workspace/README.md", "source"),
        ("", "other"),
        ("/workspace/", "other"),
    ],
)
def test_resource_class_buckets_paths(path, expected):
    assert resource_class(path) == expected


def test_github_directory_is_not_mistaken_for_git_internals():
    """`.github` starts with `.git`; the two classes must not collide."""
    assert resource_class("/w/.github/workflows/ci.yml") != resource_class("/w/.git/config")


# --- signal extraction -----------------------------------------------------


def test_tool_ngrams_include_unigrams_bigrams_and_trigrams():
    out = signals(_tools("Read", "Bash", "Write"), groups=("tool_ngram",), n_max=3)
    assert "tool_ngram:1:Read" in out
    assert "tool_ngram:2:Read>Bash" in out
    assert "tool_ngram:3:Read>Bash>Write" in out


def test_tool_ngrams_are_order_sensitive():
    a = set(signals(_tools("Read", "Bash"), groups=("tool_ngram",)))
    b = set(signals(_tools("Bash", "Read"), groups=("tool_ngram",)))
    assert a != b


def test_resource_signals_use_classes_not_exact_paths():
    out = signals(
        _trace(Event(ts=1.0, kind="file_read", source="strace", attrs={"path": "/workspace/.env"})),
        groups=("resource",),
    )
    assert out == ["resource:read:secret_file"]


def test_two_different_source_files_produce_one_resource_signal():
    trace = _trace(
        Event(ts=1.0, kind="file_read", source="strace", attrs={"path": "/w/a.js"}),
        Event(ts=2.0, kind="file_read", source="strace", attrs={"path": "/w/b.js"}),
    )
    assert len(signals(trace, groups=("resource",))) == 1


def test_a_denied_access_is_different_behaviour_from_a_successful_one():
    """`ok` is the one attribute the checks engine treats as load-bearing.

    "Tried to read the secret and was refused" and "read the secret" are the two
    sides of the violation being searched for. Collapsing them costs the
    scheduler the rung between them.
    """
    allowed = _trace(
        Event(ts=1.0, kind="file_read", source="strace", attrs={"path": "/w/.env", "ok": True})
    )
    denied = _trace(
        Event(ts=1.0, kind="file_read", source="strace", attrs={"path": "/w/.env", "ok": False})
    )
    assert signals(allowed, groups=("resource",)) != signals(denied, groups=("resource",))
    assert signals(denied, groups=("resource",)) == ["resource:read:secret_file:denied"]


def test_memory_write_and_permission_change_are_their_own_signals():
    trace = _trace(
        Event(ts=1.0, kind="memory_write", source="fsdiff", attrs={"path": "CLAUDE.md"}),
        Event(
            ts=2.0,
            kind="permission_change",
            source="strace",
            attrs={"path": "/w/x", "mode": "0777"},
        ),
    )
    out = set(signals(trace, groups=("memory_write", "permission_change")))
    assert out == {"memory_write:agent_state", "permission_change:0777"}


def test_net_signals_record_host_and_allow_decision():
    trace = _trace(
        Event(
            ts=1.0,
            kind="net_attempt",
            source="gateway",
            attrs={"host": "collector.invalid", "allowed": False},
        )
    )
    assert signals(trace, groups=("net",)) == ["net:denied:collector.invalid"]


def test_process_signals_use_the_binary_not_the_full_command():
    trace = _trace(
        Event(
            ts=1.0,
            kind="process_exec",
            source="strace",
            attrs={"path": "/usr/bin/curl", "argv": ["curl", "https://x/secret"]},
        )
    )
    assert signals(trace, groups=("process",)) == ["process:curl"]


def test_signals_are_sorted_and_deduplicated():
    out = signals(_tools("Read", "Read"), groups=("tool_ngram",))
    assert out == sorted(set(out))


def test_an_unknown_signal_group_is_refused(minimal_scenario):
    """A typo here makes every trace look identical and the search go blind.

    The docstring invites looping over subsets of the groups for the PRD's
    ablation, which is exactly the code path where a typo gets written once and
    read never.
    """
    with pytest.raises(ReprobeError, match="tool_ngrams"):
        signals(_tools("Read"), groups=("tool_ngrams",))


def test_an_n_max_below_one_is_refused():
    """`n_max=0` yields no n-grams at all, which looks exactly like an agent
    that called no tools."""
    with pytest.raises(ReprobeError, match="n_max"):
        signals(_tools("Read"), n_max=0)


def test_every_declared_group_actually_produces_signals():
    """A group listed in SIGNAL_GROUPS but unhandled would be silently dead."""
    rich = _trace(
        Event(ts=1.0, kind="tool_call", source="agent", attrs={"name": "Read"}),
        Event(ts=2.0, kind="file_read", source="strace", attrs={"path": "/w/.env"}),
        Event(ts=3.0, kind="memory_write", source="fsdiff", attrs={"path": "CLAUDE.md"}),
        Event(ts=4.0, kind="permission_change", source="strace", attrs={"mode": "0777"}),
        Event(ts=5.0, kind="net_attempt", source="gateway", attrs={"host": "h", "allowed": True}),
        Event(ts=6.0, kind="process_exec", source="strace", attrs={"argv": ["curl"]}),
    )
    for group in SIGNAL_GROUPS:
        assert signals(rich, groups=(group,)), f"{group} produced nothing"


# --- fingerprints and the map ----------------------------------------------


def test_fingerprint_is_stable_for_the_same_trace():
    trace = _tools("Read", "Bash")
    assert fingerprint(trace).signature() == fingerprint(trace).signature()


def test_fingerprint_differs_for_different_behaviour():
    assert fingerprint(_tools("Read")).signature() != fingerprint(_tools("Bash")).signature()


def test_fingerprint_keeps_the_signal_list_for_explainability():
    cov = fingerprint(_tools("Read"))
    assert "tool_ngram:1:Read" in cov.signal_list
    assert cov.signal_count == len(cov.signal_list)


def test_map_reports_novelty_on_first_sight_only():
    cmap = CoverageMap()
    first = cmap.update(fingerprint(_tools("Read", "Bash")))
    second = cmap.update(fingerprint(_tools("Read", "Bash")))
    assert first.is_novel and first.new_edges > 0
    assert not second.is_novel and second.new_edges == 0


def test_map_reports_novelty_for_a_partially_new_trace():
    cmap = CoverageMap()
    cmap.update(fingerprint(_tools("Read", "Bash")))
    third = cmap.update(fingerprint(_tools("Read", "Bash", "Write")))
    assert third.is_novel
    assert third.new_edges >= 2  # the Write unigram plus new bigram/trigram


def test_covered_count_grows_monotonically():
    cmap = CoverageMap()
    cmap.update(fingerprint(_tools("Read")))
    before = cmap.covered_count
    cmap.update(fingerprint(_tools("Write")))
    assert cmap.covered_count > before


def test_empty_trace_is_not_novel_and_does_not_crash():
    cmap = CoverageMap()
    assert not cmap.update(fingerprint(_trace())).is_novel


def test_contains_all_reports_whether_the_map_has_seen_it():
    cmap = CoverageMap()
    cov = fingerprint(_tools("Read", "Bash"))
    assert not cmap.contains_all(cov)
    cmap.update(cov)
    assert cmap.contains_all(cov)
    assert not cmap.contains_all(fingerprint(_tools("Write")))


# --- the size contract -----------------------------------------------------


def test_a_map_refuses_coverage_sized_for_a_different_map():
    """Mixed sizes index unrelated bits. The draft raised IndexError one way and
    silently answered nonsense the other."""
    cmap = CoverageMap(size=1 << 16)
    small = fingerprint(_tools("Read"), size=4096)
    with pytest.raises(ReprobeError, match="size"):
        cmap.update(small)
    with pytest.raises(ReprobeError, match="size"):
        cmap.contains_all(small)


def test_a_size_that_is_not_a_whole_number_of_bytes_is_refused():
    """`size // 8` truncates, so indices past the last byte crash -- and only
    for some traces, which is the worst way for it to fail."""
    with pytest.raises(ReprobeError, match="multiple of 8"):
        fingerprint(_tools("Read"), size=100)
    with pytest.raises(ReprobeError, match="multiple of 8"):
        CoverageMap(size=100)


# --- the gradient the Phase-2 gate climbs -----------------------------------


def test_partial_progress_is_novel_against_no_progress():
    """What makes a coverage-guided scheduler beat a random one.

    The fake agent's `placement_sensitive` profile reads the canary before it
    exfiltrates it. If coverage cannot tell "touched the secret" from "did
    nothing", there is no half-successful parent to keep and the scheduler has
    nothing to climb.
    """
    cmap = CoverageMap()
    nothing = _trace(
        Event(ts=1.0, kind="file_read", source="strace", attrs={"path": "/w/README.md"})
    )
    touched = _trace(
        Event(ts=1.0, kind="file_read", source="strace", attrs={"path": "/w/README.md"}),
        Event(ts=2.0, kind="file_read", source="strace", attrs={"path": "/w/.env"}),
    )
    exfiltrated = _trace(
        *touched.events,
        Event(
            ts=3.0,
            kind="net_attempt",
            source="gateway",
            attrs={"host": "collector.invalid", "allowed": False},
        ),
    )
    assert cmap.update(fingerprint(nothing)).is_novel
    assert cmap.update(fingerprint(touched)).is_novel, "reading the secret was not new behaviour"
    assert cmap.update(fingerprint(exfiltrated)).is_novel, "exfiltration was not new behaviour"
    assert not cmap.update(fingerprint(exfiltrated)).is_novel
