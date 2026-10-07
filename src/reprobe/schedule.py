"""R7 and R8: what to try next.

`EnergyScheduler` is the product. `RandomScheduler` is the control the PRD's
headline metric is measured against, and it is deliberately a *fair* control:
same seeds, same mutators, same budget, no memory. The only difference is that
it never learns, which is exactly the variable under test.

One confound the gate has to rule out: the guided arm reaches deep mutation
lineages and a one-mutation baseline does not, so a win could be about mutation
count rather than guidance. `RandomScheduler` therefore takes
`mutations_per_candidate`, so a depth-matched arm can be run alongside the
standard one and the two reported separately. Reporting a depth advantage as a
guidance advantage would be the easiest way to fake this whole project.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, Field

from reprobe.coverage import Coverage, CoverageMap, Novelty
from reprobe.errors import ConfigError
from reprobe.mutate import Candidate, MutationContext, mutate
from reprobe.seeds import Seed

#: Share of trials spent on a fresh seed rather than a corpus parent.
#:
#: A constant, not a decreasing function of corpus size. The draft used
#: `max(0.1, 1 / (1 + len(corpus)))`, which spends half the budget on fresh
#: seeds when the corpus holds one entry and a third when it holds two --
#: exactly the point in a run where exploiting the first good find matters
#: most, and the reason its own novelty-preference test came out at 86 of 200.
SEED_INJECTION_RATE = 0.15

#: Trials with no progress before exploration starts climbing, and how many
#: more until it reaches 1.0. "Progress" is new coverage *or* a violation --
#: a search still finding violations is working whether or not the coverage
#: map has anything left to give, and pushing it toward random at that moment
#: would be worst at exactly the point it is succeeding.
#:
#: The Phase-2 gate is why this exists. On four scenarios no mutation of any
#: corpus parent could reach the target -- the payload's position is fixed by
#: the surface template, so the gradient was unclimbable -- and a guided search
#: at a fixed 15% drew a sixth as many fresh seeds as the blind baseline and
#: lost to it, scoring an exact 0 against the baseline's 3 to 8. A search with
#: nothing left to climb should explore. Stuck, it should match the baseline;
#: it should never do worse.
STALL_TRIALS = 10
RAMP_TRIALS = 20

#: Exploration never reaches 1.0, because **a slow climb is indistinguishable
#: from no climb**. A gradient that needs two mutations composed looks stalled
#: for a long stretch before it pays, and a search that abandons its corpus
#: entirely gives up exactly then. Measured over 30 seeds on three gradient
#: shapes, with the random baseline at 1.0, 2.6 and 11.2:
#:
#:   cap   compositional      unclimbable        climbable
#:   0.15  34.9  7/30 zero    4.7  14/30 zero    24.8  1/30 zero
#:   0.70  32.1  9/30 zero    8.3   2/30 zero    25.7  0/30 zero
#:   1.00  17.8 21/30 zero    8.8   3/30 zero    26.1  0/30 zero
#:
#: Uncapped costs roughly half the compositional case to buy nothing the cap
#: does not already buy. 0.7 is a compromise across all three, not the optimum
#: of any one of them.
MAX_SEED_RATE = 0.7


class CorpusEntry(BaseModel):
    model_config = {"frozen": False}

    candidate: Candidate
    #: The coverage *signature*, not the `Coverage`. Keeping the bitmap per
    #: entry would hold 8KB per corpus member for data only ever compared by
    #: identity.
    signature: str
    trials: int = 0
    violations: int = 0
    novel_hits: int = 0
    depth: int = 0
    byte_size: int = 0

    def energy(self) -> float:
        """How much budget this entry deserves.

        Four terms, each with a reason:
          * novelty found so far -- an input that keeps opening new behaviour is
            worth more mutations. Damped by a log so one lucky input cannot
            monopolise the budget;
          * shallowness -- a short lineage is cheaper to shrink later and less
            likely to be an accumulation of junk;
          * smallness -- a small payload mutates into a more diverse
            neighbourhood than a 30KB one, and is closer to the shrunk form we
            want anyway;
          * having violated -- the neighbourhood of a working exploit is the
            best place to look for another one.
        """
        novelty_term = 1.0 + math.log1p(self.novel_hits)
        depth_term = 1.0 / (1.0 + 0.15 * self.depth)
        size_term = 1.0 / (1.0 + self.byte_size / 4000)
        violation_term = 2.0 if self.violations else 1.0
        return novelty_term * depth_term * size_term * violation_term


class Corpus(BaseModel):
    entries: list[CorpusEntry] = Field(default_factory=list)
    _by_signature: dict[str, CorpusEntry] = {}

    def __len__(self) -> int:
        return len(self.entries)

    def add(
        self,
        candidate: Candidate,
        coverage: Coverage,
        novelty: Novelty | None,
        *,
        depth: int,
        violated: bool = False,
    ) -> bool:
        """Keep the input if it reached new coverage or violated. Return whether kept.

        A behaviour already in the corpus is *folded into* its existing entry
        rather than appended beside it, violation or not. The draft appended, so
        a violation reproducing on every trial added a corpus entry per trial;
        energy-weighted sampling then collapses onto copies of one input while
        the corpus size keeps climbing, which looks like a healthy search.
        """
        signature = coverage.signature()
        existing = self._by_signature.get(signature)
        if existing is not None:
            existing.trials += 1
            existing.novel_hits += novelty.new_edges if novelty else 0
            existing.violations += 1 if violated else 0
            return False

        if not (novelty and novelty.is_novel) and not violated:
            return False

        entry = CorpusEntry(
            candidate=candidate,
            signature=signature,
            trials=1,
            novel_hits=novelty.new_edges if novelty else 0,
            violations=1 if violated else 0,
            depth=depth,
            byte_size=candidate.byte_size,
        )
        self.entries.append(entry)
        self._by_signature[signature] = entry
        return True

    def best(self) -> CorpusEntry:
        return max(self.entries, key=lambda e: e.energy())


class Scheduler(Protocol):
    name: str
    corpus: Corpus
    coverage_map: CoverageMap

    def next_candidate(self, rng: random.Random) -> Candidate: ...

    def observe(
        self,
        candidate: Candidate,
        coverage: Coverage,
        novelty: Novelty | None,
        *,
        violated: bool,
    ) -> None: ...

    def stats(self) -> dict[str, Any]: ...


def _check_surfaces(ctx: MutationContext) -> list[str]:
    """No silent fallback surface.

    The draft defaulted to `"readme"`, so a scenario whose attacker-controlled
    surface is called anything else would render every payload into a key the
    workspace never reads -- a whole run of trials against an unmodified repo,
    reported as a clean pass.
    """
    if not ctx.surfaces:
        raise ConfigError(
            "the mutation context lists no surface to place a payload in; "
            "a scenario must declare at least one attacker-controlled surface"
        )
    return list(ctx.surfaces)


def _depth_of(cand: Candidate) -> int:
    """Mutations applied since the seed.

    Lineage starts with one `from_seed` entry and gains exactly one entry per
    mutation, so depth is derivable. The draft kept a side dict keyed by
    candidate id that was never pruned and so grew for the life of the run.
    """
    return max(0, len(cand.lineage) - 1)


class EnergyScheduler:
    name = "energy"

    def __init__(
        self,
        seeds: Sequence[Seed],
        ctx: MutationContext,
        *,
        corpus: Corpus | None = None,
        map_size: int | None = None,
    ) -> None:
        self._seeds = list(seeds)
        self._ctx = ctx
        self._surfaces = _check_surfaces(ctx)
        self.corpus = corpus or Corpus()
        self.coverage_map = CoverageMap(map_size) if map_size else CoverageMap()
        self._since_progress = 0

    def seed_rate(self) -> float:
        """Share of trials to spend on a fresh seed, right now.

        Constant while the search is getting somewhere, then a linear ramp to
        full exploration once the corpus has stopped paying for itself.
        """
        over = self._since_progress - STALL_TRIALS
        if over <= 0:
            return SEED_INJECTION_RATE
        climbed = min(1.0, over / RAMP_TRIALS)
        return SEED_INJECTION_RATE + (MAX_SEED_RATE - SEED_INJECTION_RATE) * climbed

    def next_candidate(self, rng: random.Random) -> Candidate:
        if not self.corpus.entries or rng.random() < self.seed_rate():
            return self._fresh_seed(rng)
        return mutate(self.pick_parent(rng).candidate, self._ctx, rng)

    def _fresh_seed(self, rng: random.Random) -> Candidate:
        seed = rng.choice(self._seeds)
        return Candidate.from_seed(seed, surface_id=rng.choice(self._surfaces), ctx=self._ctx)

    def pick_parent(self, rng: random.Random) -> CorpusEntry:
        """Energy-weighted sampling. Public so the preference can be measured
        on its own, without the fresh-seed injections mixed in."""
        weights = [e.energy() for e in self.corpus.entries]
        return rng.choices(self.corpus.entries, weights=weights, k=1)[0]

    def observe(
        self,
        candidate: Candidate,
        coverage: Coverage,
        novelty: Novelty | None,
        *,
        violated: bool,
    ) -> None:
        self.corpus.add(candidate, coverage, novelty, depth=_depth_of(candidate), violated=violated)
        # Progress is new coverage *or* a violation. A search still finding
        # violations is working whether or not the coverage map has anything
        # left to give.
        progressed = bool(novelty and novelty.is_novel) or violated
        self._since_progress = 0 if progressed else self._since_progress + 1

    def stats(self) -> dict[str, Any]:
        return {
            "scheduler": self.name,
            "corpus_size": len(self.corpus),
            "covered_edges": self.coverage_map.covered_count,
            "seed_rate": round(self.seed_rate(), 3),
            "trials_since_progress": self._since_progress,
        }


class RandomScheduler:
    """R8: the baseline. Same seeds, same mutators, no memory."""

    name = "random"

    def __init__(
        self,
        seeds: Sequence[Seed],
        ctx: MutationContext,
        *,
        mutations_per_candidate: int = 1,
    ) -> None:
        if mutations_per_candidate < 1:
            raise ConfigError(
                f"mutations_per_candidate must be at least 1, got {mutations_per_candidate}"
            )
        self._seeds = list(seeds)
        self._ctx = ctx
        self._surfaces = _check_surfaces(ctx)
        self._mutations = mutations_per_candidate
        self.corpus = Corpus()
        self.coverage_map = CoverageMap()

    def next_candidate(self, rng: random.Random) -> Candidate:
        seed = rng.choice(self._seeds)
        cand = Candidate.from_seed(seed, surface_id=rng.choice(self._surfaces), ctx=self._ctx)
        for _ in range(self._mutations):
            cand = mutate(cand, self._ctx, rng)
        return cand

    def observe(
        self,
        candidate: Candidate,
        coverage: Coverage,
        novelty: Novelty | None,
        *,
        violated: bool,
    ) -> None:
        """Deliberately empty. Tracking coverage here would make it greybox."""

    def stats(self) -> dict[str, Any]:
        return {
            "scheduler": self.name,
            "corpus_size": len(self.corpus),
            # Read, not hardcoded: the loop updates this map so the two arms'
            # run records are comparable. Reporting zero would be a wrong
            # record, not a stronger guarantee of blindness.
            "covered_edges": self.coverage_map.covered_count,
            "mutations_per_candidate": self._mutations,
        }
