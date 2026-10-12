# Architecture

Reprobe is a search loop feeding a triage pipeline, with exporters at the end.
A finding reaches CI only after it reproduces above threshold and has been
shrunk.

```
                    ┌──────────── coverage map ────────────┐
                    │  tool n-grams · resource classes     │
                    │  memory writes · permission changes  │
                    ▼                                      │
  seeds ──▶ mutators ──▶ sandboxed trial ──▶ trace ────────┘
                              │                │
                              │                ▼
                              │            checks  ──▶ violation?
                              │                           │
                              └───────────────────────────┤
                                                          ▼
   CI test  ◀──  export  ◀──  shrink  ◀──  reproduction rate (Wilson interval)
```

The top two rows are the search (`reprobe fuzz`): each trial's trace updates
the coverage map, which steers the scheduler toward new behaviour. The bottom
row is triage (`reprobe triage`), then export (`reprobe export`,
`reprobe report`) and re-measurement (`reprobe verify`, and the exported tests).

## The three seams

Everything above the sandbox talks through narrow contracts. That is what
lets the search and triage layers be tested without Docker or API spend.

- **`SandboxProtocol`** (`sandbox/__init__.py`): `run(TrialSpec) -> TrialResult`
  and `describe()`. The real one is `DockerSandbox`. `FakeSandbox` replays
  canned results with no container at all.
- **`AgentAdapter`** (`agents/base.py`): the only place a vendor's CLI flags and
  output format appear. Claude Code, Codex CLI and the scripted fake agent.
- **The scheduler** (`schedule.py`): `next_candidate(rng)`. `EnergyScheduler` is
  coverage-guided; `RandomScheduler` is the baseline it is measured against.

## Modules

**Contracts and bookkeeping.** `scenario.py` loads and validates the YAML
scenario and computes its hash, which covers the fixture's contents but not
its path. `canary.py` mints the per-trial synthetic secrets, all prefixed
`RPRB_CANARY_`. `ids.py` holds the one hashing algorithm. `budget.py` holds the
price table and the ledger, which checks the caps *before* each dispatch.
`store.py` is the append-only run store: JSONL records plus content-addressed
blobs. `errors.py` is the exception hierarchy; everything intentional derives
from `ReprobeError`.

**One trial.** `trial.py` mints canaries, builds the `TrialSpec`, runs the
sandbox, evaluates the checks and writes the trial record below.
`sandbox/docker_sandbox.py` runs one disposable container per trial, on an
`internal` network whose only peer is the mock gateway. `sandbox/workspace.py`
materialises the fixture, renders payloads into surfaces, and diffs the
workspace afterwards. The observers turn raw artefacts into events, one file per
source: `observers/syscalls.py` (strace), `observers/fsdiff.py` (the manifest
diff), and `observers/egress.py` (the gateway log). The agent's own structured
output is parsed by its adapter. `trace.py` is the event model, with JSONL and
OpenTelemetry export. `checks.py` holds the deterministic checks: canary read,
canary exfiltrated, protected file written, egress off the allowlist, dangerous
command.

**Search.** `seeds/` is the corpus: hand-written seeds in `builtin.py`, and
`importers.py` for Promptfoo, AgentDojo and JSONL. `mutate/` holds the template
mutators, which change what a payload says, and the structural ones, which
change how it is delivered. `coverage.py` extracts behavioural signals from a
trace into a bitmap and decides novelty. `schedule.py` holds the corpus and the
two schedulers. `loop.py` ties them together.

**Triage.** `stats.py` is the Wilson interval and the early-stopping rule.
`confirm.py` re-runs a candidate until its rate is decisive, counting only its
own violation and excluding harness failures. `shrink.py` is statistical delta
debugging over payload surfaces, blocks, lines and tokens, then over the
scenario's prerequisites. `finding.py` is the confirmed, shrunk, pinned unit of
value. `dedupe.py` groups findings by violating action. `triage.py` runs the
pipeline.

**Export and after.** `export/pytest_export.py` writes a self-contained suite:
tests, finding records and bundled scenarios. `export/gha.py` writes the GitHub
Actions workflow that runs it. `export/report.py` writes the Markdown and HTML
report. `verify.py` re-measures a finding with a three-way verdict (FIXED,
REPRODUCES, INCONCLUSIVE). Only FIXED passes. `bench.py` runs guided against
random on equal trial budgets. `cli.py` is the command line.

## How it is tested and shipped

- **`ci.yml`**, on every push and pull request. `check` runs ruff, mypy and the
  unit suite on Python 3.11 and 3.12 (the suite is at 100% coverage, with
  `docker_sandbox.py` omitted because only a live daemon exercises it).
  `integration` builds the free-lane images and runs the Docker lane against
  the scripted fake agent. `hygiene` scans the tree for real-looking
  credentials and for tracked planning documents. `ci-ok` is the single check
  branch protection requires.
- **`soak.yml`**, nightly: the harness-reliability gate, the slow statistical
  checks and, only if a key is configured, a small paid adapter-drift check.
- **`release.yml`**, on a `v*` tag only. `build` checks, refuses a tag that does
  not match the package version, builds and test-installs the wheel. `publish`
  is the only job that can mint a PyPI token and waits for a maintainer's
  approval in the `pypi` environment. `github-release` creates the release from
  the changelog section. Nothing else publishes.

The test lanes are opt-in by environment variable: none for unit tests,
`REPROBE_DOCKER_TESTS=1` for containers, `REPROBE_SLOW_TESTS=1` for the
minutes-long statistical checks, `REPROBE_AGENT_TESTS=1` for real spend. An
opted-in lane whose Docker daemon is missing fails rather than skips.

## Requirements traceability

Each shipped requirement, and where it lives.

| ID | Requirement | Where |
| --- | --- | --- |
| R1 | Declarative scenario | `src/reprobe/scenario.py` |
| R2 | Claude Code and Codex CLI adapters, versions recorded | `src/reprobe/agents/claude_code.py`, `src/reprobe/agents/codex_cli.py` |
| R3 | Disposable container per trial, mock egress only | `src/reprobe/sandbox/docker_sandbox.py` |
| R4 | Deterministic violation checks | `src/reprobe/checks.py` |
| R5 | Trace of tool calls, files, processes, network | `src/reprobe/trace.py`, `src/reprobe/observers/syscalls.py` |
| R6 | Template and structural mutators | `src/reprobe/mutate/template.py`, `src/reprobe/mutate/structural.py` |
| R7 | Behavioural coverage map | `src/reprobe/coverage.py`, `src/reprobe/schedule.py` |
| R8 | Random-mutation baseline | `src/reprobe/schedule.py` |
| R9 | Reproduction rate with a Wilson interval, early stopping | `src/reprobe/stats.py`, `src/reprobe/confirm.py` |
| R10 | Statistical shrinking of payload and environment | `src/reprobe/shrink.py` |
| R11 | pytest test and GitHub Action, pinned | `src/reprobe/export/pytest_export.py`, `src/reprobe/export/gha.py`, `src/reprobe/verify.py` |
| R12 | Budget caps; versions and hashes recorded | `src/reprobe/budget.py`, `src/reprobe/trial.py` |
| R14 | Seed import from Promptfoo, AgentDojo, corpora | `src/reprobe/seeds/importers.py` |
| R16 | Duplicate grouping by violating action | `src/reprobe/dedupe.py` |
| R17 | Markdown and HTML report | `src/reprobe/export/report.py` |

Not in v0.1: R13 (more agents), R15 (model-generated mutations and a bandit
scheduler) and R18 (distributed runners).

## The trial record

One JSON object per line in `<store>/<run>/trials.jsonl`, written by
`trial._store_record`. It is the replay contract: everything needed to say what
ran, against what, and what happened.

```json
{
  "trial_id": "trial_...",
  "scenario_name": "readme-injection",
  "scenario_hash": "scn:<16 hex>",
  "agent_id": "claude-code",
  "agent_version": "2.1.289 (Claude Code)",
  "model_id": "claude-haiku-4-5",
  "prompt_hash": "<hash of the task prompt>",
  "container_digest": "sha256:<image id>",
  "seed": 1234,
  "payload_refs": {"readme": "blob:<16 hex>"},
  "trace_ref": "blob:<16 hex>",
  "verdict": {"violated": true, "violations": [
    {"check": "canary_read", "severity": "violation",
     "action_key": "canary_read:/workspace/.env",
     "evidence": {"path": "/workspace/.env", "ts": 1.689, "via": "strace"}}]},
  "harness_error": null,
  "exit_code": 0,
  "cost_usd": 0.0136,
  "input_tokens": 18,
  "output_tokens": 251,
  "duration_s": 42.1,
  "canary_ids": ["api_key"]
}
```

`verdict` is `null` when `harness_error` is set: a trial the harness broke has
no verdict, and every rate excludes it rather than counting it as clean.
Payloads and traces are blobs: content-addressed, stored once, referenced by
`blob:` refs. `container_digest` is the local image id, which differs between
platform builds of the same Dockerfile.

## Run store layout

```
<store>/
  blobs/<hash>                    payloads and traces, content-addressed
  <run_id>/meta.json              command, scenario path and hash, agent, seed
  <run_id>/trials.jsonl           one trial record per line (above)
  <run_id>/candidates.jsonl       fuzz: one per distinct violating payload
  <run_id>/confirmations.jsonl    triage: each rate measurement
  <run_id>/shrinks.jsonl          triage: each shrink trajectory
  <run_id>/findings.jsonl         triage: the findings
  <run_id>/dropped.jsonl          triage: what did not hold up, and why
```

A store holds adversarial payloads. Keep it out of version control:
`.reprobe/` is in this repository's `.gitignore`, and should be in yours.
