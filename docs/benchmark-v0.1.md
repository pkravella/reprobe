# Benchmark — v0.1

Measurements recorded at each phase gate. Numbers are from a developer machine
(Docker Desktop 29.7.2, linux/arm64 VM on macOS); treat them as order-of-
magnitude, not as a leaderboard.

## Phase 1 — harness reliability gate

**PRD milestone 1: one scenario runs 100 times with no harness failures.**

Run 2026-10-05 on the `minimal` scenario with the scripted fake agent, which
exercises the entire harness — real containers, the internal network, the mock
gateway, `strace`, the filesystem diff, and all five checks — for free. A real
model's behaviour is the one thing it does not exercise, which is not what a
*harness*-reliability gate tests.

```
uv run reprobe soak tests/data/scenarios/minimal/scenario.yaml \
    --runs 100 --agent fake-agent --model reprobe-fake --max-usd 0
```

| Metric | Value |
| --- | --- |
| Trials | 100 |
| **Harness failures** | **0** |
| Violations | 0 (the `never` profile does only the honest task) |
| Total cost | $0.0000 |
| Median trial duration | 1.86 s |
| Trial duration min / p90 / max | 1.80 / 1.96 / 2.46 s |
| Wall-clock, 100 trials (serial) | 190 s |

**Result: PASS.** `0 harness failure(s)` over 100 trials.

### `strace` overhead

Measured by timing the fake agent in its container with the `strace` entrypoint
versus `--entrypoint reprobe-fake-agent` (no tracing), 10 runs each, same
workspace and profile:

| | Mean per run |
| --- | --- |
| With `strace -f -yy -ttt` | 0.493 s |
| Without `strace` | 0.338 s |
| **Overhead** | **1.46×** |

Under the plan's 2× budget, so the `--seccomp-bpf` fallback is not needed. This
is a short, shell-heavy workload (the worst case for `strace`); a real agent's
turns are dominated by model latency, where the relative overhead is far
smaller.

### Real-agent confirmation

Not run here — it is the one unavoidable spend (~a few cents for 10 trials on
`claude-haiku-4-5`), and it needs the infra-host CONNECT tunnel that is
deliberately deferred (a real agent cannot reach its model API through the
internal-network gateway until that lands). The nightly `soak` workflow runs
the free lane on every schedule and the paid adapter-drift check only when a
key is configured. The free gate above is what runs on every commit.

## Phase 2 — search gate

_Not yet reached._

## Phase 3 — triage gate

_Not yet reached._
