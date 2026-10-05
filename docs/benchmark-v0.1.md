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

The infra-host CONNECT tunnel now exists, so a real agent *can* reach its model
API: the sandbox dual-homes the gateway onto an egress network and tunnels a
CONNECT only to the agent's declared `infra_hosts` (`api.anthropic.com` for
Claude Code), refusing every other CONNECT and keeping the agent itself on the
internal-only network. Verified end to end that an agent reaches a named host
through the tunnel while a direct dial of anything else still gets `000`.

The paid 10-trial confirmation on `claude-haiku-4-5` is still the one unavoidable
spend (~a few cents) and is run by the nightly `soak` workflow only when a key
is configured, not on every commit. The free fake-agent gate above remains the
per-commit gate. R3 now reads: no egress except a named, logged set of
model-API hosts that only the gateway can reach.

## Phase 2 — search gate

_Not yet reached._

## Phase 3 — triage gate

_Not yet reached._
