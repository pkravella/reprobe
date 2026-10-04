# Reprobe

**Turn an agent security failure into a minimal, statistically reliable regression test that runs in CI.**

Reprobe runs a coding agent on legitimate tasks in a disposable sandbox, mutates the inputs an attacker controls, and steers the search toward new agent behaviour. Each violation it finds is shrunk to the smallest payload that still fails, measured for how often it reproduces, and exported as a test pinned to the agent, model and container it was found on.

---

> ### Status: pre-implementation
>
> **There is no working code in this repository yet.** The design is finished and the
> implementation is planned task-by-task, but nothing below is built. This README
> describes what Reprobe is being built to do, not what it currently does.
>
> Follow [issues](https://github.com/pkravella/reprobe/issues) or watch the repo if you
> want to know when v0.1 lands. Early design feedback is genuinely welcome — see
> [CONTRIBUTING.md](CONTRIBUTING.md).

---

## The problem

Coding agents read surfaces an attacker controls: READMEs, issues, web pages, tool metadata, terminal output. Teams test these with a handful of hand-written injections or a catalog scanner, and get a pass or fail from a single run. That leaves three gaps:

1. **Flaky findings.** The same input passes on one run and fails on the next, so one failure proves little and one pass proves nothing.
2. **Bloated findings.** A failing payload is an entire README, and nobody knows which part triggered the behaviour.
3. **Findings that don't stick.** Nothing turns the exploit into a test, so a model, prompt or tool change can quietly reopen it.

Reprobe is a search, shrink and regression layer for exactly those three gaps. It is designed to plug into the attack catalogs that already exist rather than replace them.

## How it is meant to work

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
   CI test  ◀──  export  ◀──  shrink  ◀──  reproduction rate (Wilson 95%)
```

- **Scenario.** A YAML file: honest work for the agent, a repository fixture, which surfaces an attacker controls, synthetic secrets, and what counts as a violation.
- **Sandbox.** One disposable container per trial, on a Docker network with no route off the host. The only reachable peer is a mock gateway that logs every egress attempt and refuses anything off the allowlist.
- **Observation.** Four independent sources — the agent's own structured output, `strace` in the container, a filesystem manifest diff, and the gateway log — because an agent's self-report is not ground truth.
- **Checks.** Deterministic only: a synthetic canary read or sent, a protected file changed, egress to a host off the allowlist, a dangerous command run. No model-as-judge.
- **Search.** Template and structural mutators, with a coverage-guided scheduler that spends budget on inputs reaching new agent behaviour. A blind random-mutation mode ships alongside it as the comparison baseline.
- **Triage.** Every candidate is re-run to estimate its reproduction rate with a Wilson 95% interval, then delta-debugged down to a minimal payload — accepting a cut only when the interval's *lower bound* stays above threshold.
- **Export.** A pytest test and a GitHub Action, pinned to agent version, model id and container digest. The test asserts on the measured rate, not on one run, so it passes once the exploit genuinely stops reproducing.

## Design goals

| Goal | Target |
| --- | --- |
| Find more | At least 2× the unique reproducible failures per dollar of a random-mutation baseline, on the same scenarios |
| Prove it | Every finding ships with a shrunk payload and a reproduction rate with its 95% interval |
| Keep it fixed | Every finding exports as a CI test that fails while the exploit still reproduces above threshold |
| Test agents as shipped | Work against off-the-shelf agent CLIs without modifying them |

These are targets to measure against, not measured results. When v0.1 ships, the benchmark write-up will report what was actually achieved next to each one — including where a target was missed.

## Non-goals

- Chat-only jailbreak testing. [garak](https://github.com/NVIDIA/garak) and [PyRIT](https://github.com/Azure/PyRIT) cover it.
- Runtime protection or blocking in production.
- Proving an agent secure. Coverage is a search aid, not a guarantee.
- Contact with real external services or real credentials.
- Testing models without an agent around them.

## Prior work

Reprobe is a layer on top of the ecosystem, not a replacement for it. Credit where it is due:

| Project | What it does | What Reprobe adds |
| --- | --- | --- |
| [Promptfoo](https://www.promptfoo.dev/docs/red-team/coding-agents/) | Coding-agent attack plugins, canary-secret checks, repeated runs | Coverage-guided search, shrinking, confidence intervals. Imports its plugins as seeds |
| [AgentVigil](https://arxiv.org/abs/2505.05849) | Tree search over injection seeds for web agents | Coding-agent focus, public code |
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) | Benchmark of fixed injection tasks | Reprobe runs on it as a harness rather than competing |
| [promptmin](https://www.npmjs.com/package/promptmin), [FailMin](https://pypi.org/project/failmin/) | Delta debugging for prompts and failure traces | Shrinks the injection and its environment together, under a noisy oracle |

Several recent papers describe greybox fuzzing of agents guided by tool-call sequences or data flow. None has released code, which is a large part of why this project exists.

> **On the name.** "AgentFuzz" was already taken by a [USENIX Security 2025 paper](https://www.usenix.org/conference/usenixsecurity25/presentation/liu-fengyu),
> and `reprobe` is taken on PyPI by an unrelated activation-steering library. The
> distribution will therefore be published as **`reprobe-agents`**, while the import
> package and the CLI stay `reprobe`. "Reprobe" remains a working name pending
> trademark review — see [docs/naming.md](docs/naming.md).

## Safety

This is a tool for testing agents you own or are authorised to test. The design keeps the blast radius small by construction:

- **Synthetic secrets only.** Canaries are generated per trial with the recognisable prefix `RPRB_CANARY_`. No real credential ever enters a trial.
- **No real egress.** Trial containers run on an `internal` Docker network. Every outbound attempt lands on a local mock gateway, which logs it and refuses anything off the scenario's allowlist.
- **No inherited environment.** A container receives only the variables its agent adapter explicitly declares.
- **Bring your own key.** Reprobe never ships or proxies credentials for a hosted agent.

Reporting a vulnerability in Reprobe itself: see [SECURITY.md](SECURITY.md). Reporting a flaw you find in a third-party agent *using* Reprobe: a coordinated-disclosure guide ships with v0.1.

## Contributing

Scenarios are the most valuable thing an outside contributor can add, and they need no Python — a scenario is a YAML file plus a small repository fixture. [CONTRIBUTING.md](CONTRIBUTING.md) has the details, including the rule that contributed payloads must target synthetic canaries and never real credentials or real collection endpoints.

## Licence

[Apache-2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
