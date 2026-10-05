# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Implementation has started. The package installs and the CLI runs, but no command does anything yet.

### Added

- Package skeleton: distribution `reprobe-agents`, import package `reprobe`, console script `reprobe`. The names differ because `reprobe` is taken on PyPI; see [docs/naming.md](docs/naming.md).
- `reprobe` CLI with all five subcommands declared and stubbed (`run`, `fuzz`, `triage`, `export`, `verify`), plus `--version`.
- Error hierarchy: `ReprobeError` with `ScenarioError`, `HarnessError` and `BudgetExceeded`.
- CI runs lint, type checking and unit tests on Python 3.11 and 3.12, with the interpreter pinned per matrix leg.
- Infra-host egress tunnel: the mock gateway now tunnels an HTTPS `CONNECT` to a named set of model-API hosts and refuses all others, and the sandbox dual-homes the gateway onto an egress network so a real agent can reach its model while the agent container itself keeps no route off the internal network. The checks never treat a tunnelled infra host as a finding. This is what lets a real agent run at all; the free fake-agent lane needs none of it and still runs with no egress network.
- `reprobe run` and `reprobe soak`, and the trial orchestrator behind them: one path that mints per-trial canaries, runs the sandbox, evaluates the checks, and appends a replayable record to the run store, with the budget ledger consulted before any spend. `soak` is the Phase-1 exit gate — it runs the free fake agent in the real sandbox and fails if any trial hit a harness error. Verified: 100 trials, 0 harness failures, $0 (see [docs/benchmark-v0.1.md](docs/benchmark-v0.1.md)).
- `reprobe.checks` (R4): the five deterministic violation checks — canary read, canary exfiltration, protected-file write, off-allowlist egress, and dangerous command — each a pattern match over observed events with no model in the loop, so a verdict is reproducible. A read counts only if the open actually succeeded, infrastructure hosts are never a finding, and each violation carries a coarse action key so the same behaviour reached by different payloads groups together.
- `reprobe.sandbox.docker_sandbox` (R3): the disposable per-trial sandbox — one container on a Docker network with no route off the host, the mock gateway as its only reachable peer and DNS sinkhole, every capability dropped, and the four observers merged into one trace afterwards. The agent gets no host environment it was not granted, a timeout or an agent-reported failure is recorded as a harness error rather than a clean pass, and the container and network are always torn down. Plus `FakeSandbox`, which replays results with no container so the search and triage layers test for free, and `reprobe.observers.fsdiff`, the authoritative record of what changed on disk.
- Scripted fake agent for the free test lane: a real process in the real sandbox making real syscalls and real network calls, driven by a seeded probability model instead of a model, so the whole pipeline — Docker, the internal network, the gateway, strace, the filesystem diff, and the checks — runs end to end on every PR at no cost, with a known true reproduction rate to measure the statistics against.
- Codex CLI adapter (R2), alongside Claude Code: same seam, a completely different CLI. Codex needs a login step before it will accept an API key, its own sandbox cannot nest inside a container, and it reports cached tokens as a subset of the input total rather than separately — all three verified by running it, and all three silent failures if got wrong.
- `reprobe.agents` (R2): the agent seam — an adapter is the only place a vendor's CLI flags and output format appear, so supporting another agent is one file plus one Dockerfile. The Claude Code adapter drives the published CLI with published flags, verified against `--help` in the image rather than from documentation, and parses its streaming output into trace events and token counts. A failure the agent reports in its own stream is surfaced as a harness error instead of being mistaken for a clean run that found nothing.
- `reprobe.observers.syscalls` (R5): the syscall observer, parsing `strace -f -yy -ttt` output into process, file, network and permission events — the ground truth that does not depend on the agent reporting its own behaviour honestly. Interrupted syscalls, which is a quarter of a real log, are reconstructed from both halves rather than dropped, every event records whether the call actually succeeded so a failed open cannot read as a successful one, and writes to agent-state files are flagged separately because they change how the agent behaves on later turns. Tested against a recorded sample, not a hand-written one.
- `reprobe.observers.egress` and the mock egress gateway (R3): a forward proxy and DNS sinkhole that is the only peer a trial container can reach, logging every connection attempt — including the HTTPS ones, which arrive as `CONNECT` and carry no body — so an attempt to reach an off-allowlist host is evidence whether or not it could have succeeded. The host recomputes the allowlist decision from the log rather than trusting the gateway, which runs inside the blast radius, and canary detection covers the body, the query string and every request header, since a secret can be smuggled in any of them.
- `reprobe.sandbox`: the trial contracts — `TrialSpec`, `TrialResult`, `FsDiff`, `EgressRecord` and the `SandboxProtocol` seam that lets the search and triage layers be tested with no container and no spend. A harness failure is recorded distinctly from a clean trial, so it can never be counted as a pass.
- `reprobe.sandbox.workspace`: builds the repo the agent works in — the fixture copied, each attacker-controlled surface rendered with its payload, canaries planted, and `git init` plus one commit so the repo has history to reason about. Afterwards a content manifest diff reports exactly what changed, including symlinks, so pointing a link at a protected path cannot slip past the observer the checks treat as authoritative.
- Sandbox container images and a `make images` build: an agent-agnostic base with `strace` and a Node new enough for the agent CLIs, one image per agent CLI with its resolved version recorded inside for a finding to be pinned to, and the mock egress gateway image. The base entrypoint runs the agent under `strace` and needs no added Linux capability to do it, so trial containers can run with every capability dropped. Verified invocations per agent in [docs/agents.md](docs/agents.md).
- `reprobe.trace`: the trial trace (R5) — timestamped events from four independent observers (the agent, `strace`, a filesystem diff and the egress gateway), merged into one ordered timeline, round-tripped through JSONL, and exportable as OpenTelemetry spans that carry the events' own timestamps so a finding can be read in a trace viewer.
- `reprobe.store`: append-only run storage — JSONL records per run plus content-addressed blobs for traces and payloads, so any finding can be replayed. Safe to write from the search loop's thread pool, and a run killed mid-write stays readable.
- `reprobe.budget`: cost model and enforced budget caps (R12) — dollars, trials, concurrency and wall-clock, with per-token-kind pricing including cache reads and writes. Token counts are recorded alongside dollars so archived runs can be re-priced. Rates and provenance in [docs/pricing.md](docs/pricing.md).
- `reprobe.scenario`: declarative YAML scenarios (R1) — attacker-controlled surfaces, synthetic canary specs, egress allowlist, protected paths, policy checks and per-trial limits, with a content hash that covers the fixture's contents so a finding pins the environment it was found in. Authoring guide in [docs/scenarios.md](docs/scenarios.md).
- `reprobe.canary`: per-trial synthetic secrets (`RPRB_CANARY_` prefix) and detection that survives line-wrapping, whitespace splitting, URL encoding, and base64 or hex encoding embedded in a larger body.
- `reprobe.ids`: one canonical JSON serialisation and one content-hash algorithm for the whole project, plus `new_id` for identity. Hashes are version-tagged so the scheme can be changed without old and new values silently colliding.

### Planned for 0.1.0

- Declarative YAML scenarios with attacker-controlled surfaces and synthetic canaries.
- Agent adapters for Claude Code and Codex CLI, driven headless with recorded versions.
- Five deterministic violation checks: canary read, canary exfiltration, protected-file write, off-allowlist egress, dangerous command.
- Template and structural mutators with a coverage-guided scheduler, plus a random baseline.
- Reproduction-rate estimation with a Wilson 95% interval and early stopping.
- Delta-debugging shrinker over payload and environment, gated on the interval's lower bound.
- Export of each finding as a pytest test and a GitHub Action, pinned to agent, model and container digest.
- A pack of ten scenarios and a published guided-versus-baseline benchmark.

[Unreleased]: https://github.com/pkravella/reprobe/commits/main
