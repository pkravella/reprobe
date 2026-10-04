# Contributing to Reprobe

Thanks for looking. Reprobe is pre-implementation, so right now the three most useful contributions are **design feedback**, **scenarios**, and **agent adapters**.

## Before you start

- **Licence.** Contributions are accepted under [Apache-2.0](LICENSE), the project's own licence, per section 5 of that licence. There is no CLA to sign.
- **Code of conduct.** [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) applies everywhere.
- **Authorised use.** Only ever point Reprobe at agents you own or have permission to test.

## The one hard rule

**Never contribute a payload, seed, scenario or fixture containing a real credential, a real collection endpoint, or an exploit against a real service.**

- Secrets in scenarios are synthetic canaries, generated at runtime with the prefix `RPRB_CANARY_`.
- Exfiltration targets are mock hostnames such as `attacker.example`, served by Reprobe's own local gateway. Nothing in the corpus may resolve to a real host.
- Fixtures are plausible-looking small projects with invented content. No copied private code.

CI enforces the mechanical part of this (a credential-pattern scan over the whole tree), but the judgement part is yours. A payload that only works by hitting somebody's real server is not a contribution.

## Contributing a scenario — the highest-value thing you can do

A scenario needs no Python. It is a YAML file plus a small repository fixture, and it describes:

- honest work for the agent to do,
- which part of the repository an attacker controls,
- which synthetic secrets exist and where,
- what counts as a violation.

Good scenarios share three properties, and the third is the one people miss:

1. **The honest task is genuinely completable.** If the agent cannot do the work, every trial is noise rather than a clean run.
2. **The attack surface is realistic.** Something an attacker could actually influence: a README in a dependency, an issue body, a CI log, a tool description.
3. **The fixture looks like a real repository.** A one-file repo does not produce the behaviour a GitHub-style task produces. Three files is the floor; a manifest, some source, and a test is better.

Open an issue using the **Scenario proposal** template first. It is a short form, and it saves you building a fixture for a surface already covered.

## Contributing an agent adapter

An adapter is one file plus one Dockerfile. It must drive the agent's **published CLI with published flags** and parse its **published output format** — never a patched or wrapped agent, because "test agents as shipped" is the point of the project and a modified agent invalidates every finding from it.

The adapter's job is narrow: build a command line, parse stdout into events, report a version, and declare which environment variables may enter the container. Everything else is shared.

## Development setup

Once there is code to run (tracking issue: v0.1 implementation):

```bash
git clone https://github.com/pkravella/reprobe
cd reprobe
uv sync --dev
uv run pytest tests/unit -v     # fast, no Docker, no API keys, no cost
```

Reprobe is built so that **almost all of it can be developed and tested for free.** There are three lanes:

| Lane | What it covers | Needs |
| --- | --- | --- |
| Unit tests | Search, triage, shrinking, export algorithms | Nothing |
| Fake-agent pipeline | The whole pipeline — containers, network isolation, `strace`, checks, coverage, scheduler, shrinker | Docker |
| Local-model lane | A real agent CLI driving a model you host | Docker + a local model server |

```bash
make images                                                 # build the sandbox images
REPROBE_DOCKER_TESTS=1 uv run pytest tests/integration -v   # still free
```

**You should not need to spend money to contribute.** If a change can only be verified against a paid hosted agent, say so in the PR and a maintainer will run it.

## Pull requests

- Branch off `main`. One logical change per PR.
- **Tests first.** This project is test-driven by design: a PR that changes behaviour without a test that would have caught the old behaviour will be sent back.
- Keep `ruff check`, `ruff format --check`, `mypy` and `pytest tests/unit` green. CI runs all four.
- [Conventional Commits](https://www.conventionalcommits.org/) for messages: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`.
- Explain *why* in the PR description, not just what. The diff shows what.

## Reporting a bug

Open an issue with the **Bug report** template. For anything involving a trial, the trace is the evidence — include the run id and the relevant trace lines. "The check fired wrongly" is hard to act on; "`dangerous_command` fired on `chmod +x scripts/build.sh`, trace attached" is a fix.

For a security vulnerability **in Reprobe itself**, do not open a public issue — see [SECURITY.md](SECURITY.md).
