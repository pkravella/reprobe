# Running findings in CI

`reprobe export` turns triaged findings into a pytest suite you can keep in your
repository. Each test re-measures one exploit, and **passes only once the
exploit is shown fixed**. This page covers adopting that suite: where it goes,
what it needs, what its results mean, and when to retire a test.

## Export

From the run store that `reprobe triage` wrote, at your repository's root:

```bash
reprobe export .reprobe --dest tests/reprobe \
    --workflow .github/workflows/reprobe.yml
```

This writes:

- `tests/reprobe/test_<scenario>_<action>_<hash>.py`: one per distinct finding,
  by default one per duplicate group (`--all` writes every finding). Each
  states, in its docstring, the finding, the pinned agent, model and container,
  the recorded rate, and what the test decides.
- `tests/reprobe/reprobe_findings/<fingerprint>.json`: the finding records,
  payloads included. Payloads are data and never appear in a test file.
- `tests/reprobe/reprobe_findings/scenarios/<name>/`: a copy of each scenario
  and its fixture, so the suite runs in a repository that does not contain
  Reprobe's scenarios. Export first checks that the copy is the scenario the
  finding was measured in.
- `tests/reprobe/conftest.py`: the fixture that runs the measurement.
- The workflow, only with `--workflow`. Every scheduled run spends agent budget,
  so it is never written unasked. It builds from the Reprobe release you
  exported with (tag `v<version>`); pass `--reprobe-ref` to pin another tag or
  commit.

Re-exporting rewrites the generated files in place, and removes tests for
findings no longer exported. Tests and a `conftest.py` you wrote yourself are
left alone. Files are named by the finding's content, so the same bug from a
new triage run keeps its file name.

Commit the suite: it is the regression test. It contains attack payloads aimed
at synthetic canaries, so treat it like any other security test data.

## Running it

```bash
REPROBE_REGRESSION_TESTS=1 pytest tests/reprobe -v
```

Without `REPROBE_REGRESSION_TESTS=1`, every test **skips**, and the run ends
with a banner saying how many Reprobe tests did not run. That lets a repository
carry the suite without every contributor needing an API key or Docker.

With it set, anything that stops a finding being measured **fails** the test
instead of skipping it: a missing API key, no Docker daemon, a crashing
sandbox, an exhausted budget. A skip is green, and in CI nobody investigates a
green run.

What the run needs:

- **Docker**, and the sandbox images for the agents your findings use. The
  generated workflow builds them from a pinned Reprobe checkout.
- **The agent's API key**, and only that one: `ANTHROPIC_API_KEY` for Claude
  Code, `OPENAI_API_KEY` for Codex CLI, nothing for the fake agent. In the
  workflow these are repository secrets.

| Variable | Meaning | Default |
| --- | --- | --- |
| `REPROBE_REGRESSION_TESTS` | `1` to measure; anything else skips | unset |
| `REPROBE_MAX_USD` | dollar cap for the whole run, across every test | `5` |
| `REPROBE_SCENARIOS` | where the scenarios are | the bundled copies |
| `REPROBE_FAKE_PROFILE_OVERRIDE` | fake agent only: measure with this profile instead, such as `never` to see the suite pass | unset |

## What a result means

Each test re-measures its finding one trial at a time, until the Wilson
interval of the reproduction rate decides, or the trial cap is reached.

| Result | Meaning | Test |
| --- | --- | --- |
| **FIXED** | The whole interval is below the threshold: the exploit no longer reproduces often enough to matter | passes |
| **REPRODUCES** | The interval's lower bound is at or above the threshold: it still reproduces | fails |
| **INCONCLUSIVE** | The trial cap, the budget or the harness ran out before the interval decided | fails |

INCONCLUSIVE fails on purpose: "could not tell" is not "fixed". A crashed trial
is never counted as the agent resisting.

The default cap is 20 trials, and a fixed agent passes in 9. Set it with
`reprobe export --max-trials N`; values too small to ever decide are refused at
export. A higher cap makes a partly fixed agent more likely to settle as FIXED
or REPRODUCES rather than INCONCLUSIVE, at more cost per run. **Budget:** the
worst case per run is roughly `findings × max_trials × cost per trial`.
`REPROBE_MAX_USD` caps it hard, and a test the cap cuts short is INCONCLUSIVE,
not FIXED.

**How stable it is.** For an agent that is fixed, or still clearly
vulnerable, a test gives the same result on rerun essentially every time.
Between a true reproduction rate of about 5% and 30%, it can flip between
runs. That is the design: it would rather fail on a partial fix than pass one.
The numbers are in [benchmark-v0.1.md](benchmark-v0.1.md).

**Drift is a warning, not a failure.** A passing test raises a pytest warning
when the agent version, model or container image differs from what the finding
recorded. A model upgrade is exactly what this suite exists to check, and the
workflow rebuilds images each run, so their ids always change.

## When a test starts failing

Usually after an agent, model, prompt or tool change.

1. **Re-measure it on its own** with `reprobe verify tests/reprobe`. It prints
   each finding's verdict and rate, and any drift notes, and exits non-zero
   until every finding is FIXED.
2. **Read the verdict.** REPRODUCES means the change reopened the exploit.
   INCONCLUSIVE means the measurement did not settle: check the harness
   failures and the budget before anything else.
3. **If it reproduces**, the change is the regression. To know whether the
   exploit's minimal form has also changed, run `reprobe fuzz` and
   `reprobe triage` again against the new build, then `reprobe report` for the
   evidence.

## Retiring a finding

When the fix is real, the test passes on its own. Keep it: it now guards the
fix. Retire it only when the scenario no longer describes something you ship.
Re-export without that finding, or delete its test file and its JSON record
together. A test whose record is missing fails at collection, so a half-removed
finding is loud.

A scenario edited after export fails its tests with "scenario changed". Restore
the scenario, or re-triage and re-export against the new one.

## The workflow

`--workflow` writes a GitHub Actions workflow that:

- runs weekly (`--schedule`, cron, default Mondays 06:00 UTC), on demand, and
  on pull requests that touch the suite. It does not run for pull requests from
  forks: they get no secrets, so the suite would fail on every one;
- checks out Reprobe at the pinned ref, builds only the images your findings'
  agents need, and installs Reprobe from that checkout;
- passes only the API-key secrets those agents need, with a read-only
  `GITHUB_TOKEN`;
- sets `REPROBE_REGRESSION_TESTS=1` and `REPROBE_MAX_USD` (25 by default; edit
  it in the file).

The trials a run makes are recorded in `.reprobe/verify` in the checkout. Add
`.reprobe/` to your `.gitignore`: the store holds the payloads and traces of
every trial.
