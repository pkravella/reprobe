"""R9: reproduction rate with a 95% confidence interval.

Wilson, not Wald: with the small n we can afford (5-40 agent runs) and rates
near 0 or 1, Wald intervals go out of bounds and understate uncertainty. Wilson
is the standard small-sample choice and is what the PRD commits to.

Early stopping is deliberately simple -- stop when the interval is decisively on
one side of the threshold, when it is narrow enough to be useful, or at the cap.
A sequential test (SPRT) would save a few trials but makes the reported interval
harder to defend, and defensibility is the product.

**Everything here refuses rather than returning a wrong interval.** The whole
triage layer reads one bit off this module -- is the lower bound still above the
threshold -- so an interval that is quietly inverted or quietly impossible does
not make the shrinker worse, it makes it confidently wrong about which payload
reproduces. Counts outside `0 <= successes <= trials` and a non-positive `z`
both raise. An inverted interval in particular can satisfy `decisive_above` and
`decisive_below` at the same time, which no caller is written to survive.

The one place this module answers without evidence is zero trials, and it says
so honestly: the interval is the whole of [0, 1], neither decisive direction
holds, and `should_stop` will not stop. `point` is 0.0 there for want of a
better float; nothing reads a point estimate without the interval beside it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_THRESHOLD = 0.30
DEFAULT_Z = 1.96  # 95%


def wilson(successes: int, trials: int, z: float = DEFAULT_Z) -> tuple[float, float]:
    """The Wilson score interval for `successes` of `trials`, clamped to [0, 1].

    Zero trials is not an error -- it is the honest `(0.0, 1.0)`.
    """
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError(f"invalid counts: {successes} of {trials}")
    if z <= 0:
        raise ValueError(f"z must be positive, got {z}")
    if trials == 0:
        return (0.0, 1.0)
    n = float(trials)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True, slots=True)
class RateEstimate:
    successes: int
    trials: int
    lo: float
    hi: float
    point: float

    @property
    def width(self) -> float:
        return self.hi - self.lo

    def decisive_above(self, threshold: float) -> bool:
        """The whole interval sits at or above the threshold."""
        return self.lo >= threshold

    def decisive_below(self, threshold: float) -> bool:
        """The whole interval sits below the threshold."""
        return self.hi < threshold

    def summary(self) -> str:
        return f"{self.point:.0%} [{self.lo:.0%}, {self.hi:.0%}] ({self.successes}/{self.trials})"


def estimate(successes: int, trials: int, z: float = DEFAULT_Z) -> RateEstimate:
    lo, hi = wilson(successes, trials, z)
    return RateEstimate(
        successes=successes,
        trials=trials,
        lo=lo,
        hi=hi,
        point=(successes / trials) if trials else 0.0,
    )


@dataclass(frozen=True, slots=True)
class StopDecision:
    stop: bool
    reason: str


def should_stop(
    est: RateEstimate,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_trials: int = 5,
    max_trials: int = 40,
    target_width: float = 0.25,
) -> StopDecision:
    """Whether to stop sampling, and why.

    Order matters. Decisiveness about the threshold is the answer the caller
    actually wants, so it is checked first; `target_width` is the consolation
    prize for an interval that is narrow enough to report but will never become
    decisive. A `target_width` of 0.0 disables that rule, which is how a caller
    asks for exactly `max_trials` samples of a known-rate agent.
    """
    if est.trials < min_trials:
        return StopDecision(False, f"need at least {min_trials} trials")
    if est.decisive_above(threshold):
        return StopDecision(True, f"decisively above {threshold:.0%} (lo={est.lo:.2f})")
    if est.decisive_below(threshold):
        return StopDecision(True, f"decisively below {threshold:.0%} (hi={est.hi:.2f})")
    if est.width <= target_width:
        return StopDecision(True, f"interval width {est.width:.2f} <= {target_width:.2f}")
    if est.trials >= max_trials:
        return StopDecision(True, f"max trials {max_trials} reached")
    return StopDecision(False, f"interval straddles {threshold:.0%}, width {est.width:.2f}")


def trials_needed(
    p_guess: float, *, threshold: float, z: float = DEFAULT_Z, max_trials: int = 200
) -> int:
    """Smallest n at which a run landing exactly on `p_guess` is decisive.

    Used to tell a user what a stricter threshold costs before they pay for it.
    Decisive in either direction: the question is "when will I know", not "when
    will it pass". Returns `max_trials` when `p_guess` sits too close to the
    threshold to resolve within the cap -- which is the honest answer, not a
    failure, and is why callers should show the cap rather than the number alone.
    """
    for n in range(1, max_trials + 1):
        est = estimate(round(p_guess * n), n, z)
        if est.decisive_above(threshold) or est.decisive_below(threshold):
            return n
    return max_trials
