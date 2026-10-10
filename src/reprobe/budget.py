"""R12: caps on dollars, trials, concurrency, and wall-clock.

The numbers in `PRICES` drive the project's headline metric -- unique
reproducible failures per $100 -- so a wrong entry does not merely mis-report a
run, it corrupts the benchmark. Three rules follow from that:

1. **The table is local and dated.** A network lookup would make every trial
   depend on a pricing endpoint; a stale number that is 20% off is still a
   usable cap. `docs/pricing.md` records provenance, and the two change in the
   same commit.
2. **Raw token counts are always recorded alongside dollars**, so an archived
   run can be re-priced when the table is corrected. `Cost.repriced` does that.
3. **An unknown model raises.** Silently costing $0 for an unpriced model would
   quietly zero the denominator of the headline metric.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from reprobe.errors import BudgetExceeded


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens, by token kind."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    cache_write_usd_per_mtok: float


def _published(inp: float, out: float, cache_read: float) -> ModelPrice:
    # Cache writes bill at a premium over fresh input (1.25x for the default
    # 5-minute TTL). Scenarios here never opt into the longer TTL, which costs
    # more; if one ever does, add an explicit entry rather than scaling this.
    return ModelPrice(inp, out, cache_read, inp * 1.25)


# Verified against published per-MTok rates on 2026-10-03. See docs/pricing.md.
# Model ids are complete as published -- never append a date suffix.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": _published(4.00, 20.00, 0.20),
    "claude-opus-5": _published(5.00, 25.00, 0.50),  # cache read derived at 0.1x
    "claude-sonnet-5-5": _published(2.00, 10.00, 0.20),
    "claude-sonnet-5": _published(2.00, 10.00, 0.20),  # cache read derived
    "claude-haiku-4-5": _published(1.00, 5.00, 0.10),  # cache read derived at 0.1x
    "claude-fable-5-1": _published(10.00, 50.00, 0.25),
    # Free lanes. Present so `price()` returns 0.0 rather than raising, which
    # keeps the ledger's arithmetic honest instead of special-casing it.
    "reprobe-fake": ModelPrice(0.0, 0.0, 0.0, 0.0),
    "local": ModelPrice(0.0, 0.0, 0.0, 0.0),
}

_PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class Cost:
    """What one agent call consumed, in tokens and in dollars."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0

    @staticmethod
    def zero() -> Cost:
        return Cost()

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: Cost) -> Cost:
        return Cost(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.usd + other.usd,
        )

    __radd__ = __add__

    def repriced(self, model_id: str) -> Cost:
        """Same tokens, recomputed dollars -- for re-pricing archived runs."""
        return replace(
            self,
            usd=price(
                model_id,
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            ),
        )


def price(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Dollar cost of one agent call. Raises KeyError for an unpriced model."""
    try:
        p = PRICES[model_id]
    except KeyError:
        raise KeyError(_unpriced_message(model_id)) from None
    return (
        input_tokens * p.input_usd_per_mtok
        + output_tokens * p.output_usd_per_mtok
        + cache_read_tokens * p.cache_read_usd_per_mtok
        + cache_write_tokens * p.cache_write_usd_per_mtok
    ) / _PER_MILLION


def _unpriced_message(model_id: str) -> str:
    """Explain an unpriced model, suggesting the canonical id when one is near.

    Date-suffixed ids are the common mistake -- published ids are complete as
    they stand -- so point at the canonical form rather than only listing the
    table.
    """
    hint = ""
    trimmed = model_id.rsplit("-", 1)[0]
    if trimmed != model_id and trimmed in PRICES:
        hint = (
            f" Did you mean {trimmed!r}? Published model ids carry no date "
            f"suffix; see docs/pricing.md."
        )
    return (
        f"no price for model {model_id!r}.{hint} Known models: "
        f"{', '.join(sorted(PRICES))}. Add it to reprobe.budget.PRICES and "
        f"docs/pricing.md in the same commit -- a silently free model would zero "
        f"the denominator of the failures-per-dollar metric."
    )


@dataclass(frozen=True, slots=True)
class BudgetCaps:
    max_usd: float
    max_trials: int
    max_concurrency: int
    max_wall_seconds: int = 24 * 3600


class BudgetLedger:
    """Checked before every dispatch, updated after every trial.

    Thread-safe. The search loop dispatches trials from a thread pool, and `+=`
    is not atomic -- a lost increment would mean silently overspending the cap
    this class exists to enforce. The lock is held only for arithmetic, which is
    nothing against the seconds of latency in an agent call.

    Concurrency itself is bounded by `caps.max_concurrency`, which the loop
    passes to its executor rather than enforcing here.
    """

    def __init__(self, caps: BudgetCaps, clock: Callable[[], float] = time.monotonic) -> None:
        self.caps = caps
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._tokens = Cost.zero()
        self._trial_count = 0

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return self._tokens.usd

    @property
    def trials(self) -> int:
        with self._lock:
            return self._trial_count

    @property
    def tokens(self) -> Cost:
        """Accumulated token counts, so a whole run can be re-priced later."""
        with self._lock:
            return self._tokens

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.caps.max_usd - self.spent_usd)

    def check_can_dispatch(self) -> None:
        """Raise if any cap is reached. Called before a trial costs anything."""
        with self._lock:
            spent, count = self._tokens.usd, self._trial_count
        if self.caps.max_usd > 0:
            if spent >= self.caps.max_usd:
                raise BudgetExceeded(
                    f"usd cap reached: spent ${spent:.4f} of ${self.caps.max_usd:.2f}"
                )
        elif spent > 0:
            # `--max-usd 0` means what the CLI help says it means: this run must
            # cost nothing. The guard used to be `spent >= max_usd and max_usd >
            # 0`, which *disabled* the dollar cap at zero -- so the one value a
            # user would reach for to forbid spending was the only value that
            # permitted unlimited spending. The free lanes price at exactly
            # 0.0, so they still never trip this.
            raise BudgetExceeded(
                f"--max-usd 0 forbids any spend, but ${spent:.4f} has been spent; "
                "pass a positive cap to allow it"
            )
        if count >= self.caps.max_trials:
            raise BudgetExceeded(f"trials cap reached: {count} of {self.caps.max_trials}")
        elapsed = self._clock() - self._started
        if elapsed >= self.caps.max_wall_seconds:
            raise BudgetExceeded(
                f"wall-clock cap reached: {elapsed:.0f}s of {self.caps.max_wall_seconds}s"
            )

    def record(self, cost: Cost) -> None:
        with self._lock:
            self._tokens = self._tokens + cost
            self._trial_count += 1

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            tokens, count = self._tokens, self._trial_count
        return {
            "spent_usd": tokens.usd,
            "trials": count,
            "input_tokens": tokens.input_tokens,
            "output_tokens": tokens.output_tokens,
            "cache_read_tokens": tokens.cache_read_tokens,
            "cache_write_tokens": tokens.cache_write_tokens,
            "max_usd": self.caps.max_usd,
            "max_trials": self.caps.max_trials,
            "elapsed_seconds": self._clock() - self._started,
        }


__all__ = [
    "PRICES",
    "BudgetCaps",
    "BudgetLedger",
    "Cost",
    "ModelPrice",
    "price",
]
