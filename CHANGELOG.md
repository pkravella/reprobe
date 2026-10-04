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
- Disposable per-trial Docker sandbox on an internal network, with a mock egress gateway.
- Five deterministic violation checks: canary read, canary exfiltration, protected-file write, off-allowlist egress, dangerous command.
- Template and structural mutators with a coverage-guided scheduler, plus a random baseline.
- Reproduction-rate estimation with a Wilson 95% interval and early stopping.
- Delta-debugging shrinker over payload and environment, gated on the interval's lower bound.
- Export of each finding as a pytest test and a GitHub Action, pinned to agent, model and container digest.
- A pack of ten scenarios and a published guided-versus-baseline benchmark.

[Unreleased]: https://github.com/pkravella/reprobe/commits/main
