"""R6: the mutation layer.

Two kinds, and the distinction matters for the ablation the PRD asks for:
  * template mutators change *what the payload says* (wording, framing, role);
  * structural mutators change *how it is delivered* (position, encoding,
    formatting, how many files it is spread over).

Every mutator is pure: `(candidate, context, rng) -> candidate`. Determinism
comes from the caller's rng, so a whole search can be replayed from one seed.

Two contracts every mutator owes, both learned by breaking them:

**If `applies_to` says yes, `apply` must change the payload.** A mutator that
returns its input while appending a lineage entry tells the coverage map the
search explored somewhere it did not, tells the scheduler a child is new when it
is its parent, and leaves the shrinker a cut to try that was never made.

**`applies_to` takes the context.** Whether a mutator can run is not always a
property of the candidate alone -- splitting across surfaces needs a spare
surface, which lives on the scenario. Without the context the only options are
to lie in `applies_to` or to no-op in `apply`, and the first draft did both.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, Field

from reprobe.ids import digest
from reprobe.scenario import Scenario
from reprobe.seeds import Seed

#: A payload past this is not a more interesting test input, just a slower one.
#: Enforced in each growing mutator's `applies_to` as well as here, because the
#: shrinker and the tests call mutators directly.
MAX_PAYLOAD_BYTES = 32_000


class Mutation(BaseModel):
    model_config = {"frozen": True}

    mutator: str
    params: dict[str, Any] = Field(default_factory=dict)


class MutationContext(BaseModel):
    """Everything a mutator needs from the scenario, flattened.

    Mutators take this rather than the `Scenario` so a test can widen the
    surface list or move the canary without building a whole scenario.
    """

    model_config = {"frozen": True}

    scenario: Scenario
    canary_paths: dict[str, str] = Field(default_factory=dict)
    collector: str = "http://collector.invalid"
    surfaces: list[str] = Field(default_factory=list)

    def tokens(self) -> dict[str, str]:
        # Insertion order, and the caller builds `canary_paths` from
        # `scenario.canaries`, so "the first canary" is a stable choice rather
        # than an arbitrary one.
        first = next(iter(self.canary_paths.values()), ".env")
        return {
            "{{ canary_path }}": first,
            "{{ collector }}": self.collector,
            "{{ repo }}": self.scenario.name,
        }


class Candidate(BaseModel):
    """One set of payloads, one per surface, plus how it got here."""

    model_config = {"frozen": True}

    id: str
    payloads: dict[str, str]
    lineage: list[Mutation] = Field(default_factory=list)
    seed_ids: list[str] = Field(default_factory=list)

    @property
    def byte_size(self) -> int:
        return sum(len(v.encode("utf-8")) for v in self.payloads.values())

    @classmethod
    def rebuild(
        cls,
        payloads: dict[str, str],
        *,
        lineage: list[Mutation] | None = None,
        seed_ids: list[str] | None = None,
    ) -> Candidate:
        return cls(
            id=digest(payloads, prefix="cand"),
            payloads=payloads,
            lineage=lineage or [],
            seed_ids=seed_ids or [],
        )

    @classmethod
    def from_seed(cls, seed: Seed, *, surface_id: str, ctx: MutationContext) -> Candidate:
        text = seed.text
        for token, value in ctx.tokens().items():
            text = text.replace(token, value)
        return cls.rebuild(
            {surface_id: text},
            lineage=[Mutation(mutator="from_seed", params={"seed": seed.id})],
            seed_ids=[seed.id],
        )

    def derive(self, payloads: dict[str, str], mutation: Mutation) -> Candidate:
        return Candidate.rebuild(
            payloads, lineage=[*self.lineage, mutation], seed_ids=self.seed_ids
        )

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Candidate:
        """Copy, recomputing the id whenever the payloads change.

        `id` is the content hash of `payloads` everywhere else in the system --
        the corpus keys on it and the store addresses blobs by it. A plain
        `model_copy` would carry the original's id onto different content, so
        two different payloads would share one corpus slot.
        """
        if update and "payloads" in update and "id" not in update:
            update = {**update, "id": digest(update["payloads"], prefix="cand")}
        return super().model_copy(update=update, deep=deep)


class Mutator(Protocol):
    name: str

    def applies_to(self, cand: Candidate, ctx: MutationContext) -> bool: ...

    def apply(self, cand: Candidate, ctx: MutationContext, rng: random.Random) -> Candidate: ...


def _all() -> tuple[Mutator, ...]:
    from reprobe.mutate.structural import (
        ChangeFormatting,
        DuplicatePayload,
        EncodePayload,
        InsertBenignNoise,
        MovePlacement,
        SplitAcrossSurfaces,
        TruncateTail,
    )
    from reprobe.mutate.template import (
        AppendJustification,
        RephraseImperative,
        SubstituteTokens,
        SwapSeed,
        WrapInFrame,
    )

    return (
        SubstituteTokens(),
        RephraseImperative(),
        WrapInFrame(),
        AppendJustification(),
        SwapSeed(),
        MovePlacement(),
        ChangeFormatting(),
        EncodePayload(),
        SplitAcrossSurfaces(),
        DuplicatePayload(),
        InsertBenignNoise(),
        TruncateTail(),
    )


ALL_MUTATORS: tuple[Mutator, ...] = _all()


def mutate(
    cand: Candidate,
    ctx: MutationContext,
    rng: random.Random,
    *,
    mutators: tuple[Mutator, ...] = ALL_MUTATORS,
) -> Candidate:
    """Apply one applicable mutator chosen uniformly at random.

    Uniform choice is the P0 behaviour; R15's bandit replaces this function's
    choice step and nothing else. The size check is a backstop -- each growing
    mutator refuses in `applies_to` too -- and it rejects the whole mutation
    rather than truncating, because a payload cut mid-token is a different
    input than either the parent or the child.
    """
    usable = [m for m in mutators if m.applies_to(cand, ctx)]
    if not usable:
        return cand
    chosen = rng.choice(usable)
    out = chosen.apply(cand, ctx, rng)
    if out.byte_size > MAX_PAYLOAD_BYTES:
        return cand
    return out


__all__ = [
    "ALL_MUTATORS",
    "MAX_PAYLOAD_BYTES",
    "Candidate",
    "Mutation",
    "MutationContext",
    "Mutator",
    "mutate",
]
