import json

import pytest

from reprobe.budget import PRICES, BudgetCaps, BudgetLedger, Cost, price
from reprobe.errors import BudgetExceeded

CHEAP = "claude-haiku-4-5"


# ------------------------------------------------------------------------ Cost


def test_cost_adds_componentwise():
    total = Cost(100, 10, 0, 0, 0.5) + Cost(50, 5, 20, 7, 0.25)
    assert total.input_tokens == 150
    assert total.output_tokens == 15
    assert total.cache_read_tokens == 20
    assert total.cache_write_tokens == 7
    assert total.usd == pytest.approx(0.75)


def test_cost_zero_is_additive_identity():
    c = Cost(1, 2, 3, 4, 0.5)
    assert c + Cost.zero() == c


def test_cost_sums_over_many_trials():
    total = sum((Cost(10, 1, 0, 0, 0.01) for _ in range(100)), Cost.zero())
    assert total.input_tokens == 1000
    assert total.usd == pytest.approx(1.0)


def test_cost_total_tokens_counts_every_billable_kind():
    assert Cost(10, 1, 2, 3, 0.0).total_tokens == 16


def test_cost_is_immutable():
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        Cost(1, 1, 1, 1, 1.0).usd = 2.0


def test_repriced_keeps_tokens_and_recomputes_dollars():
    """Archived runs are re-priced when the table is corrected, which is only
    possible because raw token counts are always recorded."""
    tokens = Cost(1_000_000, 0, 0, 0, 999.0)
    assert tokens.repriced(CHEAP).usd == pytest.approx(1.0)
    assert tokens.repriced(CHEAP).input_tokens == 1_000_000


# ----------------------------------------------------------------------- price


def test_price_is_positive_and_scales_with_output():
    cheap = price(CHEAP, 1000, 100)
    dear = price(CHEAP, 1000, 10_000)
    assert 0 < cheap < dear


def test_price_is_linear_in_tokens():
    assert price(CHEAP, 2_000_000, 0) == pytest.approx(2 * price(CHEAP, 1_000_000, 0))


def test_price_of_nothing_is_zero():
    assert price(CHEAP, 0, 0, 0, 0) == 0.0


def test_price_charges_cache_reads_less_than_fresh_input():
    fresh = price(CHEAP, 10_000, 0, 0, 0)
    cached = price(CHEAP, 0, 0, 10_000, 0)
    assert cached < fresh


def test_price_charges_cache_writes_more_than_fresh_input():
    """Writing to the cache costs a premium over plain input. Omitting this
    field under-reports every cached run, and the headline metric is dollars."""
    fresh = price(CHEAP, 10_000, 0, 0, 0)
    written = price(CHEAP, 0, 0, 0, 10_000)
    assert written > fresh


@pytest.mark.parametrize("model", sorted(PRICES))
def test_every_listed_model_prices_coherently(model):
    p = PRICES[model]
    if p.input_usd_per_mtok == 0.0:
        pytest.skip("free lane")
    assert p.output_usd_per_mtok > p.input_usd_per_mtok, "output costs more than input"
    assert p.cache_read_usd_per_mtok < p.input_usd_per_mtok, "cache reads are a discount"
    assert p.cache_write_usd_per_mtok >= p.input_usd_per_mtok, "cache writes cost a premium"


def test_known_published_rates_are_what_we_charge():
    """Pinned against the published per-MTok rates. These numbers drive the
    project's headline "failures per $100" metric, so a silent drift here
    corrupts the benchmark rather than merely mis-reporting a run."""
    assert price("claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.00)
    assert price("claude-haiku-4-5", 0, 1_000_000) == pytest.approx(5.00)
    assert price("claude-sonnet-5-5", 1_000_000, 0) == pytest.approx(2.00)
    assert price("claude-sonnet-5-5", 0, 1_000_000) == pytest.approx(10.00)
    assert price("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.00)
    assert price("claude-opus-5-5", 0, 1_000_000) == pytest.approx(20.00)


def test_unknown_model_raises_rather_than_silently_costing_zero():
    with pytest.raises(KeyError, match="totally-made-up-model"):
        price("totally-made-up-model", 10, 10)


def test_a_date_suffixed_model_id_is_rejected():
    """Regression guard. Model ids are complete as published; appending a date
    produces an id the API does not serve, and an earlier draft of this project
    used `claude-haiku-4-5-20251001` throughout."""
    with pytest.raises(KeyError):
        price("claude-haiku-4-5-20251001", 10, 10)


def test_price_error_names_the_models_it_knows():
    with pytest.raises(KeyError, match="claude-haiku-4-5"):
        price("nope", 1, 1)


# ---------------------------------------------------------------------- caps


def _caps(**kw):
    defaults = {
        "max_usd": 100.0,
        "max_trials": 1000,
        "max_concurrency": 4,
        "max_wall_seconds": 3600,
    }
    return BudgetCaps(**{**defaults, **kw})


def test_fresh_ledger_can_dispatch():
    BudgetLedger(_caps()).check_can_dispatch()


def test_ledger_blocks_dispatch_once_dollars_are_spent():
    ledger = BudgetLedger(_caps(max_usd=1.0))
    ledger.check_can_dispatch()
    ledger.record(Cost(0, 0, 0, 0, 0.99))
    ledger.check_can_dispatch()
    ledger.record(Cost(0, 0, 0, 0, 0.02))
    with pytest.raises(BudgetExceeded, match="usd"):
        ledger.check_can_dispatch()


def test_ledger_blocks_dispatch_once_trials_are_spent():
    ledger = BudgetLedger(_caps(max_trials=2))
    ledger.record(Cost.zero())
    ledger.record(Cost.zero())
    with pytest.raises(BudgetExceeded, match="trials"):
        ledger.check_can_dispatch()


def test_ledger_blocks_dispatch_once_wall_clock_is_spent():
    clock = iter([0.0, 0.0, 10_000.0])
    ledger = BudgetLedger(_caps(max_wall_seconds=60), clock=lambda: next(clock))
    ledger.check_can_dispatch()
    with pytest.raises(BudgetExceeded, match="wall"):
        ledger.check_can_dispatch()


def test_a_zero_trial_cap_blocks_immediately():
    """The loop checks before dispatching, so a zero cap must spend nothing."""
    with pytest.raises(BudgetExceeded):
        BudgetLedger(_caps(max_trials=0)).check_can_dispatch()


def test_remaining_usd_never_goes_negative():
    ledger = BudgetLedger(_caps(max_usd=1.0))
    ledger.record(Cost(0, 0, 0, 0, 5.0))
    assert ledger.remaining_usd == 0.0


def test_remaining_usd_tracks_spend():
    ledger = BudgetLedger(_caps(max_usd=2.0))
    ledger.record(Cost(0, 0, 0, 0, 0.5))
    assert ledger.remaining_usd == pytest.approx(1.5)


def test_free_lane_never_trips_the_dollar_cap():
    """The fake-agent and local-model lanes price at zero, so a `--max-usd 0`
    run is meaningful rather than instantly broken."""
    ledger = BudgetLedger(_caps(max_usd=0.0, max_trials=10))
    for _ in range(5):
        ledger.check_can_dispatch()
        ledger.record(Cost(1000, 100, 0, 0, price("reprobe-fake", 1000, 100)))
    assert ledger.spent_usd == 0.0


# ------------------------------------------------------------------ accounting


def test_ledger_counts_trials_and_dollars():
    ledger = BudgetLedger(_caps())
    ledger.record(Cost(10, 1, 0, 0, 0.25))
    ledger.record(Cost(20, 2, 0, 0, 0.25))
    assert ledger.trials == 2
    assert ledger.spent_usd == pytest.approx(0.50)


def test_ledger_accumulates_token_totals_for_repricing():
    ledger = BudgetLedger(_caps())
    ledger.record(Cost(10, 1, 2, 3, 0.1))
    ledger.record(Cost(10, 1, 2, 3, 0.1))
    assert ledger.tokens.input_tokens == 20
    assert ledger.tokens.cache_write_tokens == 6


def test_snapshot_round_trips_into_json():
    ledger = BudgetLedger(_caps(max_usd=1.0, max_trials=10))
    ledger.record(Cost(1, 2, 3, 4, 0.1))
    snap = json.loads(json.dumps(ledger.snapshot()))
    assert snap["spent_usd"] == pytest.approx(0.1)
    assert snap["trials"] == 1
    assert snap["max_usd"] == 1.0
    assert "elapsed_seconds" in snap


def test_ledger_is_safe_under_concurrent_recording():
    """The search loop records from a thread pool, so lost increments would mean
    silently overspending the cap that R12 exists to enforce."""
    import threading

    ledger = BudgetLedger(_caps(max_usd=1e9, max_trials=10_000))
    threads = [
        threading.Thread(target=lambda: [ledger.record(Cost(1, 0, 0, 0, 0.01)) for _ in range(200)])
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.trials == 1600
    assert ledger.spent_usd == pytest.approx(16.0)
    assert ledger.tokens.input_tokens == 1600


def test_a_date_suffixed_id_suggests_the_canonical_form():
    """The likely mistake is a remembered date suffix, so the error points at
    the real id rather than only listing the table."""
    with pytest.raises(KeyError, match="Did you mean 'claude-haiku-4-5'"):
        price("claude-haiku-4-5-20251001", 10, 10)


def test_an_unrelated_unknown_model_gets_no_misleading_suggestion():
    with pytest.raises(KeyError) as exc:
        price("some-other-vendor-model", 10, 10)
    assert "Did you mean" not in str(exc.value)
