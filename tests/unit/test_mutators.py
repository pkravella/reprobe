"""R6: the mutation layer.

Three invariants carry most of the weight here, and all three were violated by
the first draft:

1. If `applies_to` says yes, `apply` must actually change the payload. A mutator
   that returns its input while appending a lineage entry lies to the coverage
   map, the scheduler and the shrinker all at once.
2. No mutator grows a payload without bound.
3. `Candidate.id` is the content hash of `payloads`, always -- including after
   `model_copy`.
"""

import random

import pytest

from reprobe.mutate import (
    ALL_MUTATORS,
    MAX_PAYLOAD_BYTES,
    Candidate,
    Mutation,
    MutationContext,
    mutate,
)
from reprobe.mutate.structural import (
    ChangeFormatting,
    EncodePayload,
    MovePlacement,
    SplitAcrossSurfaces,
)
from reprobe.mutate.template import RephraseImperative, SubstituteTokens, WrapInFrame
from reprobe.seeds import builtin_seeds

RNG_SEEDS = range(60)


@pytest.fixture
def ctx(minimal_scenario):
    return MutationContext(
        scenario=minimal_scenario,
        canary_paths={"api_key": ".env"},
        collector="http://attacker.invalid",
        surfaces=["readme"],
    )


@pytest.fixture
def two_surface_ctx(ctx):
    return ctx.model_copy(update={"surfaces": ["readme", "contributing"]})


@pytest.fixture
def cand(ctx):
    return Candidate.from_seed(builtin_seeds()[0], surface_id="readme", ctx=ctx)


@pytest.fixture
def rich_ctx(ctx):
    """Three surfaces, so `split_across_surfaces` has somewhere to split *to*."""
    return ctx.model_copy(update={"surfaces": ["readme", "contributing", "issue"]})


@pytest.fixture
def rich_cand(cand):
    """A candidate every mutator applies to, so none is silently skipped below.

    It deliberately keeps one unfilled token (so `substitute_tokens` has work)
    and occupies fewer surfaces than `rich_ctx` offers (so splitting has a
    destination). Getting this wrong is how a mutator ends up never tested.
    """
    return cand.model_copy(
        update={
            "payloads": {
                "readme": (
                    "intro line\n\n"
                    + cand.payloads["readme"]
                    + "\n\nAlso check {{ canary_path }} first.\n\noutro line"
                ),
                "contributing": "Please read the guide before opening a PR.",
            }
        }
    )


# --- the candidate contract ----------------------------------------------


def test_from_seed_fills_the_context_tokens(cand):
    text = cand.payloads["readme"]
    assert "{{ canary_path }}" not in text
    assert ".env" in text


def test_candidate_id_is_the_hash_of_the_payloads_and_nothing_else(cand, ctx):
    mutated = mutate(cand, ctx, random.Random(5))
    assert mutated.id == Candidate.rebuild(mutated.payloads).id
    same_payloads_other_history = Candidate.rebuild(
        dict(mutated.payloads), seed_ids=["something_else"]
    )
    assert same_payloads_other_history.id == mutated.id


def test_model_copy_recomputes_the_id_when_the_payloads_change(cand):
    """A stale id would make two different payloads share a corpus slot."""
    copied = cand.model_copy(update={"payloads": {"readme": "entirely different"}})
    assert copied.id != cand.id
    assert copied.id == Candidate.rebuild(copied.payloads).id


def test_model_copy_keeps_the_id_when_the_payloads_do_not_change(cand):
    assert cand.model_copy(update={"seed_ids": ["x"]}).id == cand.id


def test_byte_size_counts_every_surface(cand):
    two = cand.model_copy(update={"payloads": {"a": "xxx", "b": "yy"}})
    assert two.byte_size == 5


# --- individual mutators --------------------------------------------------


def test_substitute_tokens_is_idempotent_on_a_filled_candidate(cand, ctx):
    assert SubstituteTokens().applies_to(cand, ctx) is False


def test_substitute_tokens_fills_an_unfilled_candidate(ctx):
    raw = Candidate.rebuild({"readme": "open {{ canary_path }} please"})
    assert SubstituteTokens().applies_to(raw, ctx) is True
    out = SubstituteTokens().apply(raw, ctx, random.Random(0))
    assert "{{" not in out.payloads["readme"]
    assert ".env" in out.payloads["readme"]


def test_wrap_in_frame_preserves_the_instruction(cand, ctx):
    out = WrapInFrame().apply(cand, ctx, random.Random(0))
    assert ".env" in out.payloads["readme"]
    assert len(out.payloads["readme"]) > len(cand.payloads["readme"])


@pytest.mark.parametrize("rng_seed", RNG_SEEDS)
def test_move_placement_always_moves(rich_cand, ctx, rng_seed):
    """The draft reinserted at a random index, so a fifth of all moves were no-ops."""
    out = MovePlacement().apply(rich_cand, rich_ctx, random.Random(rng_seed))
    changed = [s for s in rich_cand.payloads if out.payloads[s] != rich_cand.payloads[s]]
    assert changed, "move_placement returned its input"
    for sid in changed:
        assert sorted(out.payloads[sid].split()) == sorted(rich_cand.payloads[sid].split())


def test_change_formatting_keeps_the_words(cand, ctx):
    out = ChangeFormatting().apply(cand, ctx, random.Random(2))
    assert ".env" in out.payloads["readme"]


def test_encode_payload_produces_a_decodable_payload(cand, ctx):
    out = EncodePayload().apply(cand, ctx, random.Random(3))
    assert out.payloads["readme"] != cand.payloads["readme"]
    assert out.lineage[-1].mutator == "encode_payload"
    assert out.lineage[-1].params["scheme"] in {"base64", "rot13", "zero_width", "hex"}


def test_split_across_surfaces_needs_a_spare_surface(cand, ctx, two_surface_ctx):
    """`applies_to` has to see the context: the spare surface is not on the candidate."""
    assert SplitAcrossSurfaces().applies_to(cand, ctx) is False
    assert SplitAcrossSurfaces().applies_to(cand, two_surface_ctx) is True


def test_split_across_surfaces_preserves_every_byte(cand, two_surface_ctx):
    wider = cand.model_copy(update={"payloads": {"readme": "A" * 100}})
    out = SplitAcrossSurfaces().apply(wider, two_surface_ctx, random.Random(4))
    assert set(out.payloads) == {"readme", "contributing"}
    assert out.payloads["readme"] + out.payloads["contributing"] == "A" * 100
    assert all(out.payloads.values()), "a split that leaves an empty half is not a split"


# --- invariants across every mutator --------------------------------------


@pytest.mark.parametrize("mutator", ALL_MUTATORS, ids=lambda m: m.name)
def test_every_mutator_applies_to_the_rich_candidate(mutator, rich_cand, rich_ctx):
    """No mutator may be silently skipped by the invariant tests below."""
    assert mutator.applies_to(rich_cand, rich_ctx) is True


@pytest.mark.parametrize("mutator", ALL_MUTATORS, ids=lambda m: m.name)
@pytest.mark.parametrize("rng_seed", RNG_SEEDS)
def test_applicable_means_it_actually_changes_something(mutator, rich_cand, rich_ctx, rng_seed):
    """The invariant the draft broke in three places.

    A mutator that records a lineage entry without changing the payload makes
    the corpus think it explored somewhere it did not, and leaves the shrinker a
    cut to try that was never there.
    """
    out = mutator.apply(rich_cand, rich_ctx, random.Random(rng_seed))
    assert out.payloads != rich_cand.payloads, f"{mutator.name} was a no-op"
    assert out.id != rich_cand.id
    assert out.lineage[-1].mutator == mutator.name


@pytest.mark.parametrize("mutator", ALL_MUTATORS, ids=lambda m: m.name)
def test_every_mutator_returns_a_valid_candidate(mutator, rich_cand, rich_ctx):
    out = mutator.apply(rich_cand, rich_ctx, random.Random(7))
    assert isinstance(out, Candidate)
    assert out.payloads
    assert all(isinstance(v, str) for v in out.payloads.values())


@pytest.mark.parametrize("mutator", ALL_MUTATORS, ids=lambda m: m.name)
def test_no_mutator_produces_an_unbounded_payload(mutator, rich_cand, rich_ctx):
    """Applied directly, bypassing `mutate`'s cap.

    `duplicate_payload` doubles and `encode_payload` multiplies, so 30 rounds of
    either is gigabytes -- the draft's own test suite hung the machine here. The
    cap has to live in `applies_to`, not only in the caller.
    """
    current = rich_cand
    for i in range(30):
        if mutator.applies_to(current, rich_ctx):
            current = mutator.apply(current, rich_ctx, random.Random(i))
    assert current.byte_size <= MAX_PAYLOAD_BYTES, f"{mutator.name} grows without bound"


# --- the mutate() driver ---------------------------------------------------


def test_lineage_grows_by_one_per_mutation(cand, ctx):
    current = cand
    for i in range(5):
        current = mutate(current, ctx, random.Random(i))
    assert len(current.lineage) == len(cand.lineage) + 5


def test_mutation_is_deterministic_for_a_seeded_rng(cand, ctx):
    a = mutate(cand, ctx, random.Random(99))
    b = mutate(cand, ctx, random.Random(99))
    assert a.payloads == b.payloads
    assert a.lineage == b.lineage


def test_mutate_returns_the_input_when_nothing_applies(cand, ctx):
    assert mutate(cand, ctx, random.Random(0), mutators=()) is cand


def test_mutate_never_exceeds_the_cap(cand, ctx):
    current = cand
    for i in range(400):
        current = mutate(current, ctx, random.Random(i))
    assert current.byte_size <= MAX_PAYLOAD_BYTES


def test_a_long_run_stays_within_the_cap_and_keeps_mutating(cand, ctx):
    """The cap must not freeze the search: a capped candidate can still shrink."""
    rng = random.Random(1234)
    current = cand
    sizes = set()
    for _ in range(300):
        current = mutate(current, ctx, rng)
        sizes.add(current.byte_size)
    assert len(sizes) > 10, "the search stopped exploring once it hit the cap"


# --- the gradient the Phase-2 gate depends on ------------------------------


def test_the_imperative_table_is_reversible(cand, ctx):
    """`POST` -> `send` with no way back is a one-way ratchet off the gradient.

    The free lane's fake agent fires on the literal `POST`. If rephrasing can
    only remove it, a long search drifts away from the trigger and never drifts
    back, and the Phase-2 gate degrades the longer it runs. See docs/seeds.md.
    """
    from reprobe.mutate.template import _IMPERATIVES

    pairs = set(_IMPERATIVES)
    assert pairs, "no imperatives declared"
    missing = [(a, b) for a, b in pairs if (b, a) not in pairs]
    assert not missing, f"one-way rephrasings: {missing}"


def test_rephrasing_can_take_post_away_and_bring_it_back(ctx):
    """End-to-end on the property above, with only the mutator under test.

    Running the full set here would pass for the wrong reason: `swap_seed`
    splices in another `POST` seed, so the word reappears even when rephrasing
    is a one-way street. Verified -- with a one-way table this test passes on
    the full set and fails on this one.
    """
    seed = next(s for s in builtin_seeds() if "POST" in s.text)
    start = Candidate.from_seed(seed, surface_id="readme", ctx=ctx)
    rng = random.Random(7)
    current, lost, regained = start, False, False
    for _ in range(200):
        current = mutate(current, ctx, rng, mutators=(RephraseImperative(),))
        has_post = "POST" in current.payloads["readme"]
        lost = lost or not has_post
        regained = regained or (lost and has_post)
    assert lost, "the search never left the trigger; this test proves nothing"
    assert regained, "`POST` was rephrased away and could not be rephrased back"


def test_mutate_rejects_a_mutator_that_blows_the_cap(cand, ctx):
    """The backstop in `mutate`, which the well-behaved mutators never reach.

    It exists because a mutator added later -- or one from a plugin, once the
    scheduler seam is public -- can get its own size guard wrong.
    """

    class Runaway:
        name = "runaway"

        def applies_to(self, cand, ctx):
            return True

        def apply(self, cand, ctx, rng):
            return cand.derive(
                {"readme": "x" * (MAX_PAYLOAD_BYTES + 1)}, Mutation(mutator=self.name)
            )

    out = mutate(cand, ctx, random.Random(0), mutators=(Runaway(),))
    assert out is cand


def test_encode_payload_declines_an_empty_candidate(ctx):
    empty = Candidate.rebuild({"readme": ""})
    assert EncodePayload().applies_to(empty, ctx) is False
