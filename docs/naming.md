# Naming

## Decision

| Kind | Name | Where it appears |
| --- | --- | --- |
| **Distribution** | `reprobe-agents` | `pip install reprobe-agents`, `[project] name` in `pyproject.toml`, PyPI |
| **Import package** | `reprobe` | `src/reprobe/`, `import reprobe`, every module path |
| **Console script** | `reprobe` | `reprobe fuzz`, `reprobe triage`, … |
| **Project name** | Reprobe | README, docs, this repository |

The two names differ because the one we wanted was taken, not by preference. Nothing in the source refers to `reprobe-agents`; it is one line in `pyproject.toml`.

## Why

Checked 2026-10-03:

| Registry | Name | Result |
| --- | --- | --- |
| PyPI | `reprobe` | **Taken.** `reprobe` 0.1.0, "Linear probes and activation steering for transformer models", published 2026-03-25 by `levashi` ([GitHub](https://github.com/levashi/reprobe)) |
| PyPI | `reprobe-agents` | Free |
| npm | `reprobe` | Free |

An unrelated project holds `reprobe` on PyPI, so the distribution name had to change. The import package did not have to, and could not take the same form regardless: **a hyphen is not legal in a Python identifier**, so `import reprobe-agents` is a syntax error. A distribution name and an import name are different things, and keeping the import name short and clean is worth the small asymmetry.

`-agents` over the alternatives (`-dev`, `-fuzz`, `-sec`) because it names the domain the tool works in. `reprobe-dev` was considered and rejected: sitting on PyPI beside a *different* project literally named `reprobe`, it would read as a development build of that one.

## How it is wired

`uv init` infers the module directory from the distribution name and would look for `src/reprobe_agents/`. One line corrects it:

```toml
[project]
name = "reprobe-agents"

[project.scripts]
reprobe = "reprobe.cli:app"

[tool.uv.build-backend]
module-name = "reprobe"
```

Verified end to end on uv 0.12.23: the wheel ships `reprobe/` with `reprobe_agents-*.dist-info`, `import reprobe` resolves, `importlib.metadata.version("reprobe-agents")` resolves, package data files inside the module are included, and the `reprobe` console script runs.

## Still open

- **Trademark.** Not cleared. This is not a blocker for an Apache-2.0 release, and the PRD lists it as an open question. If a clearance problem appears, the project name and the distribution name change together; the import package need not.
- **Prior art on the name.** "AgentFuzz" was unusable — it is a [USENIX Security 2025 paper](https://www.usenix.org/conference/usenixsecurity25/presentation/liu-fengyu). "Reprobe" now also collides with an activation-steering library on PyPI, which is a reason to keep an eventual rename cheap: do not scatter the string `reprobe-agents` anywhere but `pyproject.toml`.

## If the name has to change again

1. Change `[project] name` and this file. Leave `[tool.uv.build-backend] module-name` alone.
2. Nothing else moves. Exported regression tests pin the agent, model and container digest — not the distribution name — so existing findings keep working.
