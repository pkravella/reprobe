## What and why

<!-- What does this change, and why is it the right change? The diff shows what;
     this section is for the why. -->

## How it was verified

<!-- Which lane did you run? Paste the command and the result.
     Reprobe is test-driven: a behaviour change needs a test that would have
     caught the old behaviour. -->

- [ ] `uv run pytest tests/unit -v`
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy`
- [ ] `REPROBE_DOCKER_TESTS=1 uv run pytest tests/integration -v` (if the harness changed)
- [ ] Not verifiable without a paid hosted agent — please run it for me

## Checklist

- [ ] A test covers this change
- [ ] No real credentials, real hostnames, or real collection endpoints added anywhere
- [ ] `CHANGELOG.md` updated under *Unreleased* (skip for internal refactors)
- [ ] If this changes a check, a scenario, or the sandbox: I have said below what it means for already-exported findings
