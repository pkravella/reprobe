"""Re-measure a finding and decide whether it still reproduces.

The one piece of Reprobe that runs inside somebody else's CI, through the
`conftest.py` an export writes. Its job is to turn "is this exploit still
live?" into a pass or a fail that means the same thing every time.

**Three outcomes, and only one of them passes.** Triage keeps a finding when the
lower bound of its rate clears the threshold: the burden of proof is on "this
reproduces". A regression test must reverse that burden. If it failed only when
the lower bound cleared the threshold and passed otherwise, then a test with too
few trials to show anything would pass -- and it did: at 10 trials, a true-0.35
exploit (the kind triage accepts 31% of the time) passed 91% of runs. So:

* ``FIXED`` -- the whole interval sits below the threshold (`hi < threshold`).
  The only outcome that passes.
* ``REPRODUCES`` -- the whole interval sits at or above it (`lo >= threshold`).
* ``INCONCLUSIVE`` -- the trial cap, the budget or the harness ran out first.
  Fails, with its own message, because "could not tell" is not "fixed".

Sampling is sequential: after each usable trial the interval is checked and the
loop stops the moment it is decisive. A fixed agent passes in 9 trials at the
default threshold, a reliable exploit fails in 2. At the default 20-trial cap,
exactly (by dynamic programming over the counts, see the tests):

    true rate   0.00   0.05   0.15   0.35   0.60   1.00
    passes      100%    85%    37%   2.8%     0%     0%

The flapping zone is a partial fix between roughly 5% and 30%, which is the
right side to be unstable on. The 2.8% at 0.35 is the cost of peeking after
every trial (optional stopping), and it grows slowly with the cap: 3.9% at 40.
As in `reprobe.stats`, the interval is nominally 95% per fixed sample and is
not a calibrated interval under this stopping rule; what is defensible is the
decision, and the decision errs towards failing.

A harness failure is never a clean trial. It is excluded from the counts and
spends an attempt, so a sandbox that crashes every time ends INCONCLUSIVE rather
than passing on an interval of [0, 1] -- or, worse, on `estimate(0, 0)`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from reprobe.errors import BudgetExceeded, ConfigError
from reprobe.finding import Finding
from reprobe.stats import RateEstimate, estimate

DEFAULT_MAX_TRIALS = 20


class Status(StrEnum):
    FIXED = "fixed"
    REPRODUCES = "reproduces"
    INCONCLUSIVE = "inconclusive"


def judge(est: RateEstimate, *, threshold: float) -> Status | None:
    """The verdict an interval supports, or None while it straddles."""
    if est.decisive_above(threshold):
        return Status.REPRODUCES
    if est.decisive_below(threshold):
        return Status.FIXED
    return None


def check_decidable(*, threshold: float, max_trials: int) -> None:
    """Refuse a configuration in which one of the verdicts can never be reached.

    Too few trials for a fixed agent to pass makes a test that fails forever and
    reads as a live exploit; too few to show reproduction mislabels a live
    exploit as merely inconclusive. Both are cheaper to refuse at export time
    than to discover in a CI log.
    """
    if not 0.0 < threshold < 1.0:
        raise ConfigError(f"threshold must be strictly between 0 and 1, got {threshold}")
    if max_trials < 1:
        raise ConfigError(f"max_trials must be at least 1, got {max_trials}")
    if not estimate(0, max_trials).decisive_below(threshold):
        raise ConfigError(
            f"a fixed agent could never pass: even 0 of {max_trials} trials leaves an upper "
            f"bound of {estimate(0, max_trials).hi:.3f}, not below the {threshold:.0%} "
            "threshold; raise max_trials"
        )
    if not estimate(max_trials, max_trials).decisive_above(threshold):
        raise ConfigError(
            f"reproduction could never be shown: even {max_trials} of {max_trials} trials "
            f"leaves a lower bound of {estimate(max_trials, max_trials).lo:.3f}, under the "
            f"{threshold:.0%} threshold; raise max_trials or lower the threshold"
        )


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status
    estimate: RateEstimate
    threshold: float
    harness_failures: int = 0
    #: Why sampling stopped, when that is not simply "the interval was decisive".
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.status is Status.FIXED

    def summary(self) -> str:
        line = (
            f"{self.status.name}: {self.estimate.summary()} against a "
            f"{self.threshold:.0%} threshold"
        )
        if self.harness_failures:
            line += f"; {self.harness_failures} harness failure(s) excluded"
        if self.reason:
            line += f"; {self.reason}"
        return line


def run_sequential(
    trial: Callable[[], bool | None],
    *,
    threshold: float,
    max_trials: int = DEFAULT_MAX_TRIALS,
) -> Outcome:
    """Call `trial` until the interval is decisive or `max_trials` attempts are spent.

    `trial` returns True when the finding's violation reproduced, False when the
    trial ran cleanly without it, and None when the harness failed. It may raise
    `BudgetExceeded`, which ends sampling as INCONCLUSIVE.
    """
    check_decidable(threshold=threshold, max_trials=max_trials)
    successes = usable = harness_failures = 0
    reason = f"no decision within {max_trials} attempts"
    for _ in range(max_trials):
        try:
            result = trial()
        except BudgetExceeded as exc:
            reason = f"budget exhausted: {exc}"
            break
        if result is None:
            harness_failures += 1
            continue
        usable += 1
        successes += int(result)
        est = estimate(successes, usable)
        status = judge(est, threshold=threshold)
        if status is not None:
            return Outcome(status, est, threshold, harness_failures)
    return Outcome(
        Status.INCONCLUSIVE, estimate(successes, usable), threshold, harness_failures, reason
    )


def load_finding(path: Path, *, fingerprint: str) -> Finding:
    """Read an exported finding, refusing one that no longer matches its test.

    The test file names the finding by fingerprint, which covers the payloads,
    the violating actions and the scenario hash. A JSON file edited afterwards
    would make the test measure something its own docstring does not describe.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"finding file {path} does not exist")
    try:
        finding = Finding.from_record(json.loads(path.read_text()))
    except (ValueError, KeyError, TypeError) as exc:  # pydantic's ValidationError is a ValueError
        raise ConfigError(f"{path} is not a valid exported finding: {exc}") from exc
    if finding.fingerprint() != fingerprint:
        raise ConfigError(
            f"{path.name} has fingerprint {finding.fingerprint()} but its test expects "
            f"{fingerprint}; the finding was edited after export -- re-export it instead"
        )
    return finding


class FindingRunner:
    """Re-runs exported findings against a sandbox.

    The measurement itself (one sandbox trial per call of `run_sequential`'s
    `trial`) is not built yet, and says so: an opted-in exported test must fail
    loudly rather than pass on a measurement that never happened.
    """

    def __init__(
        self,
        scenarios_root: Path,
        *,
        max_usd: float,
        store_root: Path | None = None,
    ) -> None:
        self._root = Path(scenarios_root)
        self._max_usd = max_usd
        self._store_root = Path(store_root or ".reprobe-verify")

    def check(self, path: Path, *, fingerprint: str, threshold: float, max_trials: int) -> Outcome:
        finding = load_finding(path, fingerprint=fingerprint)
        return run_sequential(self._trial(finding), threshold=threshold, max_trials=max_trials)

    def _trial(self, finding: Finding) -> Callable[[], bool | None]:
        raise NotImplementedError(
            f"reprobe: re-measuring a finding ({finding.title()}) is not built yet"
        )


__all__ = [
    "DEFAULT_MAX_TRIALS",
    "FindingRunner",
    "Outcome",
    "Status",
    "check_decidable",
    "judge",
    "load_finding",
    "run_sequential",
]
