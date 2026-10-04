# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Pre-implementation. The repository currently contains project scaffolding, licence and contribution guidance only; no Reprobe code has been written yet.

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
