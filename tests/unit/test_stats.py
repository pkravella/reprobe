import pytest

from reprobe.stats import (
    DEFAULT_THRESHOLD,
    estimate,
    should_stop,
    trials_needed,
    wilson,
)


def test_wilson_brackets_the_point_estimate():
    lo, hi = wilson(5, 10)
    assert lo < 0.5 < hi


def test_wilson_is_bounded_to_zero_one():
    assert wilson(0, 10)[0] == 0.0
    assert wilson(10, 10)[1] == 1.0


def test_wilson_narrows_as_trials_grow():
    narrow = wilson(50, 100)
    wide = wilson(5, 10)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_wilson_of_zero_trials_is_the_whole_interval():
    assert wilson(0, 0) == (0.0, 1.0)


def test_wilson_matches_a_known_value():
    # 2 of 10, z=1.96: the Wilson interval is approximately (0.0567, 0.5098).
    # Verified independently against the score-test definition of the interval
    # -- the set of p where |p_hat - p| <= z*sqrt(p(1-p)/n) -- located by
    # bisection rather than by this module's closed form.
    lo, hi = wilson(2, 10)
    assert lo == pytest.approx(0.0567, abs=0.002)
    assert hi == pytest.approx(0.5098, abs=0.002)


def test_wilson_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        wilson(11, 10)
    with pytest.raises(ValueError):
        wilson(-1, 10)
    with pytest.raises(ValueError):
        wilson(2, -10)


@pytest.mark.parametrize("z", [0.0, -1.96])
def test_wilson_rejects_a_non_positive_z(z):
    """A non-positive z yields an inverted interval (lo > hi), and an inverted
    interval can be 'decisively above' and 'decisively below' at the same time.
    Refuse it rather than report a stop decision about nonsense."""
    with pytest.raises(ValueError):
        wilson(5, 10, z=z)


def test_estimate_reports_decisiveness_above_the_threshold():
    est = estimate(20, 20)
    assert est.decisive_above(DEFAULT_THRESHOLD)
    assert not est.decisive_below(DEFAULT_THRESHOLD)


def test_estimate_reports_decisiveness_below_the_threshold():
    est = estimate(0, 30)
    assert est.decisive_below(DEFAULT_THRESHOLD)
    assert not est.decisive_above(DEFAULT_THRESHOLD)


def test_estimate_near_the_threshold_is_not_decisive_either_way():
    est = estimate(3, 10)  # point 0.30, interval straddles it
    assert not est.decisive_above(DEFAULT_THRESHOLD)
    assert not est.decisive_below(DEFAULT_THRESHOLD)


def test_estimate_of_no_trials_claims_nothing():
    est = estimate(0, 0)
    assert (est.lo, est.hi) == (0.0, 1.0)
    assert not est.decisive_above(DEFAULT_THRESHOLD)
    assert not est.decisive_below(DEFAULT_THRESHOLD)


def test_estimate_summary_shows_the_interval_and_the_counts():
    assert estimate(3, 10).summary() == "30% [11%, 60%] (3/10)"


def test_should_stop_waits_for_the_minimum_number_of_trials():
    assert not should_stop(estimate(2, 2), threshold=DEFAULT_THRESHOLD, min_trials=5).stop


def test_should_stop_early_when_clearly_above():
    decision = should_stop(estimate(8, 8), threshold=DEFAULT_THRESHOLD, min_trials=5)
    assert decision.stop and "above" in decision.reason


def test_should_stop_early_when_clearly_below():
    decision = should_stop(estimate(0, 12), threshold=DEFAULT_THRESHOLD, min_trials=5)
    assert decision.stop and "below" in decision.reason


def test_should_stop_at_the_max_trial_cap():
    decision = should_stop(estimate(10, 30), threshold=DEFAULT_THRESHOLD, max_trials=30)
    assert decision.stop and "max" in decision.reason


def test_should_stop_when_the_interval_is_narrow_enough():
    # 30/100 is (0.219, 0.396): narrow, and still straddling the threshold.
    # That combination is the only thing the width rule is for -- an interval
    # narrow enough to be useful that will never become decisive.
    est = estimate(30, 100)
    assert not est.decisive_above(DEFAULT_THRESHOLD)
    assert not est.decisive_below(DEFAULT_THRESHOLD)
    decision = should_stop(est, threshold=DEFAULT_THRESHOLD, max_trials=200, target_width=0.25)
    assert decision.stop and "width" in decision.reason


def test_should_not_stop_while_still_ambiguous_and_wide():
    decision = should_stop(
        estimate(3, 10),
        threshold=DEFAULT_THRESHOLD,
        min_trials=5,
        max_trials=40,
        target_width=0.1,
    )
    assert not decision.stop


def test_a_zero_target_width_disables_the_width_rule():
    """Task 22 measures a known-rate agent by pinning min_trials == max_trials
    and target_width=0.0. The width rule must not cut that short."""
    decision = should_stop(
        estimate(9, 20), threshold=DEFAULT_THRESHOLD, min_trials=30, max_trials=30, target_width=0.0
    )
    assert not decision.stop


def test_trials_needed_grows_as_the_true_rate_approaches_the_threshold():
    # A monotone chain, not a single comparison: at 0.35 the answer is the cap,
    # so a two-point assertion would only be re-testing the cap.
    counts = [trials_needed(p, threshold=0.3) for p in (0.9, 0.6, 0.5, 0.4)]
    assert counts == sorted(counts)
    assert len(set(counts)) == len(counts)


def test_trials_needed_also_grows_approaching_the_threshold_from_below():
    assert trials_needed(0.05, threshold=0.3) < trials_needed(0.2, threshold=0.3)


def test_trials_needed_is_capped():
    assert trials_needed(0.301, threshold=0.3, max_trials=50) == 50


def test_a_bound_just_inside_the_threshold_does_not_render_as_the_threshold():
    """`wilson(0, 9)` has hi = 0.2991, which is decisively below 30% -- but at
    two decimals the message read "decisively below 30% (hi=0.30)", a
    contradiction, and a bound sitting just inside the threshold is precisely
    the case a reader goes looking for."""
    decision = should_stop(estimate(0, 9), threshold=DEFAULT_THRESHOLD, min_trials=5)
    assert decision.stop
    assert "hi=0.299" in decision.reason
