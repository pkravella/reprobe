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
- `reprobe.scenario`: declarative YAML scenarios (R1) — attacker-controlled surfaces, synthetic canary specs, egress allowlist, protected paths, policy checks and per-trial limits, with a content hash that covers the fixture's contents so a finding pins the environment it was found in. Authoring guide in [docs/scenarios.md](docs/scenarios.md).
- `reprobe.canary`: per-trial synthetic secrets (`RPRB_CANARY_` prefix) and detection that survives line-wrapping, whitespace splitting, URL encoding, and base64 or hex encoding embedded in a larger body.
- `reprobe.ids`: one canonical JSON serialisation and one content-hash algorithm for the whole project, plus `new_id` for identity. Hashes are version-tagged so the scheme can be changed without old and new values silently colliding.

### Planned for 0.1.0

- Declarative YAML scenarios with attacker-controlled surfaces and synthetic canaries.
- Agent adapters for Claude Code and Codex CLI, driven headless with recorded versions.
- Disposable per-trial Docker sandbox on an internal network, with a mock egress gateway.
- Four deterministic violation checks: canary read, canary exfiltration, protected-file write, off-allowlist egress, dangerous command.
- Trace capture from four independent observers, exportable as OpenTelemetry spans.
- Template and structural mutators with a coverage-guided scheduler, plus a random baseline.
- Reproduction-rate estimation with a Wilson 95% interval and early stopping.
- Delta-debugging shrinker over payload and environment, gated on the interval's lower bound.
- Export of each finding as a pytest test and a GitHub Action, pinned to agent, model and container digest.
- A pack of ten scenarios and a published guided-versus-baseline benchmark.

[Unreleased]: https://github.com/pkravella/reprobe/commits/main
