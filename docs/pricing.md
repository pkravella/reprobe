# Pricing

These numbers drive the project's headline metric — unique reproducible failures per $100 — so a wrong entry does not merely mis-report a run, it corrupts the benchmark.

**Update this file and `PRICES` in `src/reprobe/budget.py` in the same commit. Never one without the other.**

## Rates

USD per million tokens. Verified **2026-10-03** against published first-party rates.

| Model id | Input | Output | Cache read | Cache write |
| --- | --- | --- | --- | --- |
| `claude-opus-5-5` | $4.00 | $20.00 | $0.20 | $5.00 |
| `claude-opus-5` | $5.00 | $25.00 | $0.50 † | $6.25 |
| `claude-sonnet-5-5` | $2.00 | $10.00 | $0.20 | $2.50 |
| `claude-sonnet-5` | $2.00 | $10.00 | $0.20 † | $2.50 |
| `claude-haiku-4-5` | $1.00 | $5.00 | $0.10 † | $1.25 |
| `claude-fable-5-1` | $10.00 | $50.00 | $0.25 | $12.50 |
| `reprobe-fake` | $0.00 | $0.00 | $0.00 | $0.00 |
| `local` | $0.00 | $0.00 | $0.00 | $0.00 |

**†** Cache-read rate derived at the documented ≈0.1× input, not separately published for that model. Replace with the published figure when one is available.

**Cache write** is derived at 1.25× input, the documented premium for the default 5-minute TTL. The 1-hour TTL costs more (≈2×); no scenario here opts into it. If one ever does, add an explicit entry rather than rescaling.

## Model ids are complete as published

Never append a date suffix. `claude-haiku-4-5-20251001` is **not** a valid id — an early draft of this project used it throughout, and `price()` would have raised on every call. There is a regression test for exactly that string.

## Why the table is local

A pricing lookup over the network would make every trial depend on an external endpoint. A stale number that is 20% off is still a usable budget cap; an unreachable endpoint is a broken run.

The mitigation for staleness is that **raw token counts are recorded alongside dollars for every trial**, so an archived run can be re-priced when this table is corrected:

```python
Cost(input_tokens=30_000, output_tokens=8_000, usd=0.0).repriced("claude-haiku-4-5")
```

`BudgetLedger.tokens` exposes the same totals for a whole run.

## An unpriced model raises

`price()` raises `KeyError` for a model not in the table, naming the models it knows. This is deliberate: a model that silently cost $0 would zero the denominator of the failures-per-dollar metric, which is the one number the project is judged on.

Adding a model means adding a row here and an entry in `PRICES`.

## What a trial actually costs

Measured shape of one coding-agent trial on `claude-haiku-4-5` — roughly 30K fresh input, 120K cache reads, 30K cache writes, 8K output:

```
one trial                ~$0.12
100-trial soak           ~$12
3,600-trial benchmark   ~$430
```

Two things follow. First, **cache accounting is not a rounding error**: ignoring cache writes under-reports that trial by about 31%. Second, this is why the gates in the implementation plan run on the free lanes — the fake agent and a local model both price at $0.00, so a `--max-usd 0` run is meaningful rather than instantly broken.

Set `--max-usd` from a measured cost per trial rather than from these estimates, and let the ledger stop the run.
