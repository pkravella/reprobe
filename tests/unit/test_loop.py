"""The search loop: corpus + mutators + sandbox + coverage, tied to `run_trial`.

This is the first task where the whole Phase-2 stack runs end to end, so it is
also where a mistake in any of it shows up as "the search just does not find
anything". The gradient tests at the bottom are the in-silico version of the
PRD's headline claim; they run on `FakeSandbox` and cost nothing.
"""

import random
import time

import pytest

from reprobe.budget import BudgetCaps
from reprobe.errors import BudgetExceeded, ConfigError
from reprobe.loop import FuzzConfig, fuzz
from reprobe.mutate import Candidate
from reprobe.sandbox.fake import FakeSandbox
from reprobe.store import RunStore
from tests.support.results import broken_result, clean_result, leaky_result


def _config(minimal_scenario, scheduler="energy", trials=40, concurrency=1, **kw):
    kw.setdefault("agent_id", "claude-code")
    kw.setdefault("model", "claude-haiku-4-5")
    return FuzzConfig(
        scenario=minimal_scenario,
        scheduler=scheduler,
        seed=7,
        caps=BudgetCaps(max_usd=10.0, max_trials=trials, max_concurrency=concurrency),
        **kw,
    )


def _gradient_sandbox(trigger="POST"):
    """A fake agent with one weakness, and visible partial progress toward it.

    It leaks when the payload says POST *and* names the canary path within the
    first 300 characters -- the free lane's `placement_sensitive` condition.
    Short of that it still behaves differently depending on what the payload
    contains, which is the premise coverage-guided fuzzing rests on: a payload
    that gets the agent to do something new is worth keeping even when it has
    not broken anything yet.

    Without those rungs the trace is one of two fixed shapes, the corpus never
    exceeds two entries, and the test measures a crippled search.
    """

    def behaviour(spec):
        text = "\n".join(spec.payloads.values())
        head = text[:300]
        if trigger in head and ".env" in head:
            return leaky_result(canary=spec.canaries[0].value)
        tools = ["Read"]
        if trigger in text:
            tools.append("Bash")
        if ".env" in text:
            tools.append("Grep")
        if trigger in head:
            tools.append("WebFetch")
        if ".env" in head:
            tools.append("Glob")
        return clean_result(tools=tuple(tools))

    return FakeSandbox(behaviour)


# --- the loop's bookkeeping ------------------------------------------------


def test_loop_stops_at_the_trial_cap(minimal_scenario, tmp_path):
    out = fuzz(
        _config(minimal_scenario, trials=12), sandbox=_gradient_sandbox(), store=RunStore(tmp_path)
    )
    assert out.trials == 12


@pytest.mark.parametrize("concurrency", [1, 2, 4, 8])
def test_the_trial_cap_is_exact_at_every_concurrency(minimal_scenario, tmp_path, concurrency):
    """R12 says caps are enforced, not advisory.

    The ledger counts a trial when it *records a cost*, which happens after the
    trial finishes, so dispatching against the ledger alone overshoots by up to
    `concurrency - 1`. The loop knows how many it dispatched, so it enforces
    this cap itself.

    The sandbox is deliberately slow. With an instant fake, every trial
    finishes before the next dispatch and the overshoot never shows up -- the
    bug is real but unobservable, which is the same as having no test.
    """

    def slow(spec):
        time.sleep(0.02)
        return clean_result()

    out = fuzz(
        _config(minimal_scenario, trials=10, concurrency=concurrency),
        sandbox=FakeSandbox(slow),
        store=RunStore(tmp_path),
    )
    assert out.trials == 10
    assert len(list(RunStore(tmp_path).read(out.run_id, "trials"))) == 10


def test_loop_records_every_trial_in_the_store(minimal_scenario, tmp_path):
    store = RunStore(tmp_path)
    out = fuzz(_config(minimal_scenario, trials=10), sandbox=_gradient_sandbox(), store=store)
    assert len(list(store.read(out.run_id, "trials"))) == 10


def test_loop_finds_the_planted_weakness(minimal_scenario, tmp_path):
    out = fuzz(
        _config(minimal_scenario, trials=60), sandbox=_gradient_sandbox(), store=RunStore(tmp_path)
    )
    assert out.violations


def test_loop_records_candidate_findings_in_the_store(minimal_scenario, tmp_path):
    store = RunStore(tmp_path)
    out = fuzz(_config(minimal_scenario, trials=60), sandbox=_gradient_sandbox(), store=store)
    recorded = list(store.read(out.run_id, "candidates"))
    assert len(recorded) == len(out.violations)
    assert all("payloads" in r and "action_keys" in r for r in recorded)


def test_a_candidate_finding_carries_its_lineage_for_replay(minimal_scenario, tmp_path):
    store = RunStore(tmp_path)
    out = fuzz(_config(minimal_scenario, trials=60), sandbox=_gradient_sandbox(), store=store)
    record = next(iter(store.read(out.run_id, "candidates")))
    assert record["lineage"] and record["seed_ids"]
    assert record["action_keys"]


def test_loop_deduplicates_candidates_by_payload(minimal_scenario, tmp_path):
    """The same payload violating twice is one candidate finding, not two."""
    always = FakeSandbox(lambda s: leaky_result(canary=s.canaries[0].value))
    out = fuzz(_config(minimal_scenario, trials=20), sandbox=always, store=RunStore(tmp_path))
    ids = [v.candidate.id for v in out.violations]
    assert len(ids) == len(set(ids))


def test_harness_failures_are_counted_and_do_not_stop_the_loop(minimal_scenario, tmp_path):
    flaky = FakeSandbox(
        lambda s: broken_result() if len(s.payloads.get("readme", "")) % 2 else clean_result()
    )
    out = fuzz(_config(minimal_scenario, trials=20), sandbox=flaky, store=RunStore(tmp_path))
    assert out.trials == 20
    assert out.harness_failures > 0


def test_a_harness_failure_never_reaches_the_coverage_map(minimal_scenario, tmp_path):
    """A broken trial has no trace worth believing; fingerprinting it would
    teach the scheduler that crashing is new behaviour worth pursuing."""
    broken = FakeSandbox(lambda s: broken_result())
    out = fuzz(_config(minimal_scenario, trials=15), sandbox=broken, store=RunStore(tmp_path))
    assert out.harness_failures == 15
    assert out.scheduler_stats["covered_edges"] == 0
    assert out.scheduler_stats["corpus_size"] == 0


def test_budget_cap_in_dollars_stops_the_loop_early(minimal_scenario, tmp_path):
    expensive = FakeSandbox(lambda s: clean_result(usd=1.0))
    config = _config(minimal_scenario, trials=100).model_copy(
        update={"caps": BudgetCaps(max_usd=3.0, max_trials=100, max_concurrency=1)}
    )
    out = fuzz(config, sandbox=expensive, store=RunStore(tmp_path))
    assert out.trials <= 4
    assert out.cost_usd <= 4.0


def test_the_same_seed_produces_the_same_trial_sequence(minimal_scenario, tmp_path):
    def payloads_of(path):
        out = fuzz(
            _config(minimal_scenario, trials=15),
            sandbox=_gradient_sandbox(),
            store=RunStore(tmp_path / path),
        )
        return [v.candidate.payloads for v in out.violations]

    assert payloads_of("a") == payloads_of("b")


def test_the_run_record_names_the_scheduler_and_the_coverage_groups(minimal_scenario, tmp_path):
    store = RunStore(tmp_path)
    out = fuzz(
        _config(minimal_scenario, trials=5, scheduler="random"),
        sandbox=_gradient_sandbox(),
        store=store,
    )
    meta = store.meta(out.run_id)
    assert meta["scheduler"] == "random"
    assert meta["coverage_groups"]
    assert meta["caps"]["max_trials"] == 5


# --- configuration that must not fail quietly ------------------------------


def test_an_unknown_coverage_group_is_refused_before_any_spend(minimal_scenario, tmp_path):
    config = _config(minimal_scenario, trials=5, coverage_groups=("tool_ngrams",))
    sandbox = _gradient_sandbox()
    with pytest.raises(ConfigError, match="tool_ngrams"):
        fuzz(config, sandbox=sandbox, store=RunStore(tmp_path))
    assert sandbox.calls == [], "a misconfigured run must cost nothing"


def test_an_unknown_agent_profile_is_refused_before_any_spend(minimal_scenario, tmp_path):
    config = _config(minimal_scenario, trials=5, agent_profile="does_not_exist")
    sandbox = _gradient_sandbox()
    with pytest.raises(ConfigError, match="does_not_exist"):
        fuzz(config, sandbox=sandbox, store=RunStore(tmp_path))
    assert sandbox.calls == []


def test_the_agent_profile_reaches_the_sandbox(minimal_scenario, tmp_path):
    """Without this the Phase-2 gate cannot select `placement_sensitive`, and
    every gate trial runs against the default profile instead."""
    sandbox = FakeSandbox(lambda s: clean_result())
    fuzz(
        _config(
            minimal_scenario,
            trials=3,
            agent_id="fake-agent",
            model="reprobe-fake",
            agent_profile="placement_sensitive",
        ),
        sandbox=sandbox,
        store=RunStore(tmp_path),
    )
    import json

    from reprobe.agents.fake_agent import PROFILES

    assert sandbox.calls
    for spec in sandbox.calls:
        assert (
            json.loads(spec.env_overrides["REPROBE_FAKE_PROFILE"])
            == (PROFILES["placement_sensitive"])
        )
    # Per-trial seeds, not the run seed: a flaky profile that answered the same
    # way every trial would give the rate estimator nothing to measure.
    assert len({spec.env_overrides["REPROBE_FAKE_SEED"] for spec in sandbox.calls}) == 3


def test_a_scenario_with_no_surfaces_is_refused(minimal_scenario, tmp_path):
    """Nothing for a payload to go in means nothing to search; without this the
    run would dispatch trials against an unmodified repository."""
    bare = minimal_scenario.model_copy(update={"surfaces": []})
    sandbox = _gradient_sandbox()
    with pytest.raises(ConfigError, match="surface"):
        fuzz(_config(bare, trials=5), sandbox=sandbox, store=RunStore(tmp_path))
    assert sandbox.calls == []


def test_a_trial_that_lost_the_race_for_the_budget_is_not_a_harness_failure():
    """Only a race produces this: the loop checks the budget, submits, and
    another trial exhausts it before this one starts. It is not a harness
    failure and not a result -- it never ran -- so counting it either way would
    corrupt the soak gate's "zero harness failures" number. Driven directly
    because the race cannot be forced deterministically through `fuzz`.
    """
    import threading
    from concurrent.futures import Future

    from reprobe.loop import _drain

    lost: Future = Future()
    lost.set_exception(BudgetExceeded("usd cap reached"))
    absorbed: list[object] = []
    _drain(
        {lost: Candidate.rebuild({"readme": "x"})},
        lambda c, r: absorbed.append(r),
        threading.Lock(),
        all_of_them=True,
    )
    assert absorbed == []


# --- the PRD's central claim, in silico ------------------------------------


def _arm(minimal_scenario, tmp_path, scheduler, seed, trials=80):
    config = _config(minimal_scenario, scheduler=scheduler, trials=trials).model_copy(
        update={"seed": seed}
    )
    out = fuzz(config, sandbox=_gradient_sandbox(), store=RunStore(tmp_path / f"{scheduler}{seed}"))
    return out.violating_trials


def _floor(minimal_scenario, trials, seed):
    """Seeds only, no mutation and no memory.

    The control that rules out "guided" meaning "declined to mutate": a blind
    baseline handicaps itself, because a random mutation usually destroys the
    trigger (Task 19). Four of the twenty-four builtin seeds already satisfy
    the condition, so this scores about one trial in six.
    """
    from reprobe.mutate import MutationContext
    from reprobe.seeds import builtin_seeds

    ctx = MutationContext(
        scenario=minimal_scenario,
        canary_paths={"api_key": ".env"},
        surfaces=[s.id for s in minimal_scenario.surfaces],
    )
    rng = random.Random(seed)
    hits = 0
    for _ in range(trials):
        cand = Candidate.from_seed(rng.choice(builtin_seeds()), surface_id="readme", ctx=ctx)
        head = "\n".join(cand.payloads.values())[:300]
        hits += "POST" in head and ".env" in head
    return hits


def test_guided_search_beats_the_random_baseline_on_a_planted_gradient(minimal_scenario, tmp_path):
    """The PRD's central claim, against a fake agent whose weakness requires
    *composing* two things: the payload must say POST and the canary path must
    land in the first 300 bytes.

    Measured as violating trials, not unique payloads. A guided search revisits
    a payload that works, so counting distinct payloads understates it, and a
    blind search scatters, so counting them flatters it. Collapsing payloads
    that reach the same violating action is R16's job, in triage.

    Three arms over several seeds: a single run of a stochastic search proves
    nothing, and two arms cannot tell guidance from not-mutating.
    """
    trials, seeds = 150, range(8)
    guided = [_arm(minimal_scenario, tmp_path, "energy", s, trials) for s in seeds]
    blind = [_arm(minimal_scenario, tmp_path, "random", s, trials) for s in seeds]
    floor = [_floor(minimal_scenario, trials, s) for s in seeds]

    # Observed at this budget: 1.75x the blind baseline, 1.47x the floor, 7 of
    # 8 paired wins. The thresholds sit below those with room, because a change
    # to the mutators or the seed corpus should move this test's margin, not
    # flip its result.
    assert sum(guided) > sum(blind) * 1.4, (guided, blind)
    assert sum(guided) > sum(floor) * 1.25, (guided, floor)
    assert sum(1 for g, b in zip(guided, blind, strict=True) if g > b) >= 6, (guided, blind)


def test_the_planted_gradient_is_not_trivially_winnable(minimal_scenario, tmp_path):
    """If the baseline already finds it most of the time there is no gradient."""
    blind = _arm(minimal_scenario, tmp_path, "random", 0, 150)
    assert 0 < blind < 75


def test_the_run_reports_both_violation_counts(minimal_scenario, tmp_path):
    """A run that violates repeatedly on one payload and one that violates once
    on many are different results, and the gate needs to tell them apart."""
    always = FakeSandbox(lambda s: leaky_result(canary=s.canaries[0].value))
    out = fuzz(_config(minimal_scenario, trials=12), sandbox=always, store=RunStore(tmp_path))
    assert out.violating_trials == 12
    assert 0 < len(out.violations) <= 12
