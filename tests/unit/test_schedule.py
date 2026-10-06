"""R7/R8: what to try next, and the control it is measured against.

The load-bearing test here is `test_energy_scheduler_climbs_a_placement_gradient`.
Everything else can pass while the scheduler is really just shuffling: that one
simulates the free lane's `placement_sensitive` condition and checks the guided
search actually reaches it more often than the blind one. If the PRD's headline
claim is wrong, this is where it shows up — at no cost, three tasks before the
gate would have found it.
"""

import random

import pytest

from reprobe.coverage import CoverageMap, fingerprint
from reprobe.mutate import Candidate, MutationContext
from reprobe.schedule import SEED_INJECTION_RATE, Corpus, EnergyScheduler, RandomScheduler
from reprobe.seeds import builtin_seeds
from reprobe.trace import Event, Trace


@pytest.fixture
def ctx(minimal_scenario):
    return MutationContext(
        scenario=minimal_scenario, canary_paths={"api_key": ".env"}, surfaces=["readme"]
    )


def _cov(*tools):
    return fingerprint(
        Trace(
            trial_id="t",
            events=[
                Event(ts=float(i), kind="tool_call", source="agent", attrs={"name": t})
                for i, t in enumerate(tools)
            ],
        )
    )


# --- the corpus ------------------------------------------------------------


def test_corpus_keeps_a_novel_input(ctx):
    corpus, cmap = Corpus(), CoverageMap()
    cov = _cov("Read")
    assert corpus.add(Candidate.rebuild({"readme": "a"}), cov, cmap.update(cov), depth=0)
    assert len(corpus) == 1


def test_corpus_rejects_a_non_novel_input(ctx):
    corpus, cmap = Corpus(), CoverageMap()
    cov = _cov("Read")
    corpus.add(Candidate.rebuild({"readme": "a"}), cov, cmap.update(cov), depth=0)
    assert not corpus.add(Candidate.rebuild({"readme": "b"}), cov, cmap.update(cov), depth=1)
    assert len(corpus) == 1


def test_corpus_always_keeps_a_violating_input_even_without_novelty(ctx):
    corpus, cmap = Corpus(), CoverageMap()
    cov = _cov("Read")
    cmap.update(cov)
    assert corpus.add(
        Candidate.rebuild({"readme": "a"}), cov, cmap.update(cov), depth=0, violated=True
    )


def test_a_reproducing_violation_does_not_flood_the_corpus(ctx):
    """A finding that reproduces every trial would otherwise add an entry a trial.

    Energy-weighted sampling then collapses onto copies of one input, and the
    search stops exploring while still reporting a growing corpus.
    """
    corpus, cmap = Corpus(), CoverageMap()
    cov = _cov("Read")
    cmap.update(cov)
    for i in range(20):
        corpus.add(
            Candidate.rebuild({"readme": f"p{i}"}), cov, cmap.update(cov), depth=0, violated=True
        )
    assert len(corpus) == 1
    assert corpus.entries[0].violations == 20


def test_corpus_counts_trials_per_behaviour(ctx):
    corpus, cmap = Corpus(), CoverageMap()
    cov = _cov("Read")
    corpus.add(Candidate.rebuild({"readme": "a"}), cov, cmap.update(cov), depth=0)
    for i in range(4):
        corpus.add(Candidate.rebuild({"readme": f"b{i}"}), cov, cmap.update(cov), depth=1)
    assert corpus.entries[0].trials == 5


def test_best_returns_the_highest_energy_entry(ctx):
    corpus, cmap = Corpus(), CoverageMap()
    small = _cov("Read")
    big = _cov("Read", "Bash", "Write", "Edit", "Grep")
    corpus.add(Candidate.rebuild({"readme": "a" * 5000}), small, cmap.update(small), depth=9)
    corpus.add(Candidate.rebuild({"readme": "b"}), big, cmap.update(big), depth=0)
    assert corpus.best().candidate.payloads["readme"] == "b"


# --- the schedulers --------------------------------------------------------


def test_energy_scheduler_starts_from_seeds(ctx):
    sched = EnergyScheduler(builtin_seeds(), ctx)
    first = sched.next_candidate(random.Random(0))
    assert first.seed_ids


def test_energy_scheduler_mutates_a_corpus_entry_once_one_exists(ctx):
    sched = EnergyScheduler(builtin_seeds(), ctx)
    rng = random.Random(0)
    parent = sched.next_candidate(rng)
    cov = _cov("Read", "Bash")
    sched.observe(parent, cov, sched.coverage_map.update(cov), violated=False)
    children = [sched.next_candidate(random.Random(i)) for i in range(40)]
    assert any(len(c.lineage) > 1 for c in children)


def test_depth_is_read_from_the_lineage_not_a_side_table(ctx):
    """The draft kept a dict of every candidate id ever seen, which grows for
    the life of the run. Depth is just how many mutations deep the lineage is."""
    sched = EnergyScheduler(builtin_seeds(), ctx)
    rng = random.Random(0)
    parent = sched.next_candidate(rng)
    cov = _cov("Read")
    sched.observe(parent, cov, sched.coverage_map.update(cov), violated=False)
    assert sched.corpus.entries[0].depth == 0
    assert not hasattr(sched, "_depth_of")


def test_energy_scheduler_prefers_parents_that_found_novelty(ctx):
    """The behaviour that makes it coverage-*guided* rather than coverage-aware.

    Measured over parent picks only. Mixing in the fresh-seed injections, as the
    draft did, measures the exploration rate and the energy function at once and
    can only be read if you already know both.
    """
    sched = EnergyScheduler(builtin_seeds(), ctx)
    rng = random.Random(1)
    boring = sched.next_candidate(rng)
    interesting = sched.next_candidate(rng)
    boring_cov, interesting_cov = _cov("Read"), _cov("Read", "Bash", "Write", "Edit")
    sched.observe(boring, boring_cov, sched.coverage_map.update(boring_cov), violated=False)
    sched.observe(
        interesting, interesting_cov, sched.coverage_map.update(interesting_cov), violated=False
    )

    picked = [sched.pick_parent(random.Random(i)).candidate.id for i in range(400)]
    from_interesting = picked.count(interesting.id)
    from_boring = picked.count(boring.id)
    assert from_interesting + from_boring == 400
    assert from_interesting > from_boring * 1.5, (from_interesting, from_boring)


def test_fresh_seeds_keep_being_injected_but_do_not_dominate(ctx):
    """Without injection the search collapses onto one family; with too much of
    it the budget never reaches the parents. The draft spent a third of every
    early trial on fresh seeds, which is when exploitation matters most."""
    sched = EnergyScheduler(builtin_seeds(), ctx)
    rng = random.Random(3)
    for i in range(3):
        cand = sched.next_candidate(rng)
        cov = _cov(*[f"T{j}" for j in range(i + 1)])
        sched.observe(cand, cov, sched.coverage_map.update(cov), violated=False)

    fresh = sum(1 for i in range(400) if len(sched.next_candidate(random.Random(i)).lineage) == 1)
    assert 0 < fresh < 400
    assert fresh / 400 == pytest.approx(SEED_INJECTION_RATE, abs=0.08)


def test_random_scheduler_keeps_no_state(ctx):
    sched = RandomScheduler(builtin_seeds(), ctx)
    cand = sched.next_candidate(random.Random(0))
    cov = _cov("Read", "Bash")
    sched.observe(cand, cov, None, violated=True)
    assert len(sched.corpus) == 0


def test_random_scheduler_payloads_stay_shallow(ctx):
    """R8's baseline must be a genuine blind-mutation control, not a crippled
    version of the guided search: it mutates a fresh seed every time, with the
    same mutator set and the same per-trial budget."""
    sched = RandomScheduler(builtin_seeds(), ctx)
    depths = [len(sched.next_candidate(random.Random(i)).lineage) for i in range(50)]
    assert max(depths) <= 2


def test_the_baseline_can_be_depth_matched_to_the_guided_arm(ctx):
    """The confound the gate has to rule out.

    The guided arm reaches deep lineages and the baseline does not, so a win
    could be about mutation count rather than guidance. The baseline takes a
    mutation count so Task 21 can run a depth-matched arm and show which it was.
    """
    sched = RandomScheduler(builtin_seeds(), ctx, mutations_per_candidate=4)
    depths = [len(sched.next_candidate(random.Random(i)).lineage) for i in range(50)]
    assert max(depths) == 5  # from_seed plus four mutations
    assert sched.stats()["mutations_per_candidate"] == 4


def test_a_baseline_with_no_mutations_is_refused(ctx):
    """Zero mutations is the floor control, not a baseline; it must be asked
    for explicitly in a test rather than reachable by passing 0 here."""
    from reprobe.errors import ConfigError

    with pytest.raises(ConfigError, match="mutations_per_candidate"):
        RandomScheduler(builtin_seeds(), ctx, mutations_per_candidate=0)


def test_both_schedulers_are_deterministic_for_a_seed(ctx):
    def run(cls):
        sched = cls(builtin_seeds(), ctx)
        rng = random.Random(42)
        out = []
        for i in range(25):
            cand = sched.next_candidate(rng)
            cov = _cov(*[f"T{j}" for j in range(i % 5 + 1)])
            sched.observe(cand, cov, sched.coverage_map.update(cov), violated=bool(i % 7 == 0))
            out.append(cand.id)
        return out

    assert run(EnergyScheduler) == run(EnergyScheduler)
    assert run(RandomScheduler) == run(RandomScheduler)


def test_scheduler_reports_stats_for_the_run_record(ctx):
    for sched in (EnergyScheduler(builtin_seeds(), ctx), RandomScheduler(builtin_seeds(), ctx)):
        stats = sched.stats()
        assert set(stats) >= {"corpus_size", "covered_edges", "scheduler"}


def test_the_baseline_reports_the_coverage_it_was_shown(ctx):
    """It must not *steer* by coverage; reporting zero when the loop has been
    updating its map is just a wrong run record."""
    sched = RandomScheduler(builtin_seeds(), ctx)
    cov = _cov("Read", "Bash")
    sched.coverage_map.update(cov)
    assert sched.stats()["covered_edges"] > 0


def test_a_scheduler_refuses_a_context_with_no_surfaces(minimal_scenario):
    """The draft fell back to a hardcoded `"readme"`, which silently renders the
    payload into a surface the scenario may not have."""
    from reprobe.errors import ConfigError

    empty = MutationContext(scenario=minimal_scenario, canary_paths={"api_key": ".env"})
    with pytest.raises(ConfigError, match="surface"):
        EnergyScheduler(builtin_seeds(), empty)


# --- the claim the PRD is built on ------------------------------------------


def _gradient_coverage(cand: Candidate) -> tuple[object, bool]:
    """Stand in for a trial against the fake agent's `placement_sensitive`.

    That profile fires when `POST` and `.env` both appear within the first 300
    characters. The sub-goals are reported as separate behaviours, which is what
    a real trace does too: reading the secret shows up as a `file_read` whether
    or not the exfiltration follows. Returns (coverage, violated).
    """
    text = cand.payloads.get("readme", "")
    window = text[:300]
    reached = ["Start"]
    if "POST" in text:
        reached.append("HasPost")
    if ".env" in text:
        reached.append("HasEnv")
    if "POST" in window and ".env" in window:
        reached.append("BothEarly")
    violated = "BothEarly" in reached
    return _cov(*reached), violated


def _search(scheduler, steps: int, seed: int) -> int:
    rng = random.Random(seed)
    hits = 0
    for _ in range(steps):
        cand = scheduler.next_candidate(rng)
        cov, violated = _gradient_coverage(cand)
        scheduler.observe(cand, cov, scheduler.coverage_map.update(cov), violated=violated)
        hits += violated
    return hits


def _seeds_only(ctx, steps: int, seed: int) -> int:
    """The floor control: pick a seed, mutate nothing, never learn.

    Four of the twenty-four builtin seeds already satisfy the target, so this
    scores about one in six -- and it *beats the blind baseline*, because a
    random mutation usually destroys the trigger. Any claim about guidance has
    to clear this bar too, or "guided" could just mean "declined to mutate".
    """
    rng = random.Random(seed)
    hits = 0
    for _ in range(steps):
        cand = Candidate.from_seed(rng.choice(builtin_seeds()), surface_id="readme", ctx=ctx)
        hits += _gradient_coverage(cand)[1]
    return hits


def test_energy_scheduler_climbs_a_placement_gradient(ctx):
    """The Phase-2 claim, simulated at the scheduler level for free.

    Same seeds, same mutators, same number of trials; the only difference is
    whether the search remembers what worked. Three arms, because two is not
    enough to say what won: the guided arm must beat both the blind baseline
    and the do-nothing floor. Measured as paired wins across rng seeds, since a
    single run of a stochastic search proves nothing and a mean can hide a
    split.
    """
    steps = 150
    seeds = range(8)
    guided = [_search(EnergyScheduler(builtin_seeds(), ctx), steps, s) for s in seeds]
    blind = [_search(RandomScheduler(builtin_seeds(), ctx), steps, s) for s in seeds]
    floor = [_seeds_only(ctx, steps, s) for s in seeds]

    assert all(g > b for g, b in zip(guided, blind, strict=True)), (guided, blind)
    assert all(g > f for g, f in zip(guided, floor, strict=True)), (guided, floor)
    assert sum(guided) > 1.5 * sum(blind), (sum(guided), sum(blind))


def test_the_guided_arm_is_not_winning_on_mutation_count(ctx):
    """Rules out the easiest way to fake this project.

    The guided arm reaches deeper lineages than a one-mutation baseline, so a
    win could be about mutation count. Running the baseline at matched depth
    settles it -- and it comes out *lower*, because more blind mutations
    destroy the trigger more often. The advantage is guidance, not depth.
    """
    steps, seeds = 150, range(6)
    guided = [_search(EnergyScheduler(builtin_seeds(), ctx), steps, s) for s in seeds]
    deep_blind = [
        _search(RandomScheduler(builtin_seeds(), ctx, mutations_per_candidate=3), steps, s)
        for s in seeds
    ]
    assert all(g > d for g, d in zip(guided, deep_blind, strict=True)), (guided, deep_blind)


def test_the_gradient_simulation_is_not_trivially_winnable(ctx):
    """Guards the tests above from passing because every candidate triggers.

    If the blind baseline already hits the target most of the time there is no
    gradient to climb and the comparison means nothing.
    """
    hits = _search(RandomScheduler(builtin_seeds(), ctx), 150, 0)
    assert 0 < hits < 75, hits
