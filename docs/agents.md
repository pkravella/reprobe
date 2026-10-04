# Agents

Verified invocations for every agent Reprobe drives. Everything here was run on
a real machine and the output recorded; nothing is inferred from documentation.
**Re-verify before relying on it** — these are someone else's release artifacts
and they move.

Each row of the table is what an `AgentAdapter` (Task 11, Task 12) encodes. The
adapter is the only place a vendor's CLI flags appear.

| | Claude Code | Codex CLI |
| --- | --- | --- |
| npm package | `@anthropic-ai/claude-code` | `@openai/codex` |
| Version verified | **2.1.289** | **0.160.0** |
| Image | `reprobe/claude-code:dev` | `reprobe/codex-cli:dev` |
| Declared node engine | `>=22.0.0` | `>=16` |
| Version recorded at | `/home/agent/.reprobe-agent-version` | same |
| Recorded string | `2.1.289 (Claude Code)` | `codex-cli 0.160.0` |

Verified 2026-10-04 on Docker 29.7.2, linux/arm64.

## Resolving the versions

```bash
docker run --rm node:22-slim sh -lc 'npm view @anthropic-ai/claude-code version'   # 2.1.289
docker run --rm node:22-slim sh -lc 'npm view @openai/codex version'              # 0.160.0
```

Both package names in the plan were correct. `@openai/codex` additionally
publishes per-platform dist-tags (`linux-arm64`, `darwin-x64`, …); installing
plain `@openai/codex` resolves the right platform build through optional
dependencies, which is what the image does.

## Why the base image is `node:22-bookworm-slim`

Claude Code declares `engines: {node: ">=22.0.0"}`. Debian bookworm's own
`nodejs` package is **18.20.4**, so the plan's `debian:bookworm-slim` base would
run the agent on an engine it does not claim to support.

Measured, so the record is accurate: on Node 18 `npm install -g` **succeeds**
with only an `npm WARN EBADENGINE` warning, and both `claude --help` and a full
`claude -p "hi" --output-format json` run behave **indistinguishably** from Node
22. This is therefore a latent risk, not an observed break. It is still the
wrong runtime to carry into the Phase-1 gate, which requires zero harness
failures across 100 trials — an unsupported engine is an avoidable variable, and
`node:22-bookworm-slim` is still Debian 12, so every apt package is unchanged.

## Pinning for a reproducible build

`latest` by default, so a fresh clone gets a current agent. Pin explicitly when
a finding must be reproducible:

```bash
make images CLAUDE_CODE_VERSION=2.1.289 CODEX_VERSION=0.160.0
```

`make digests` prints the image id each exported finding is pinned to (R11).
Note that an image id is **per-platform**: an image built on arm64 and one built
on amd64 have different ids from the same Dockerfile, so a finding's container
pin identifies a platform build, not just a recipe.

## `strace` needs no added capability

The plan and the Phase-1 handoff both state that the sandbox requires
`cap_add=["SYS_PTRACE"]`, called "the only added capability". **It does not.**

`strace` forks the agent itself and traces a *direct child* via
`PTRACE_TRACEME`, which standard permissions already allow. `CAP_SYS_PTRACE` is
only required to attach to a process you do not own (`strace -p <pid>`).
Measured, all as the unprivileged `agent` user:

| Run flags | Result |
| --- | --- |
| docker defaults | works, 9 syscall lines |
| `--cap-add SYS_PTRACE` | works, 9 lines |
| `--cap-drop ALL` | works, 9 lines |
| `--cap-drop ALL --cap-add SYS_PTRACE` | works, 9 lines |
| `--cap-drop ALL --security-opt no-new-privileges` | works, 9 lines |

Fork-following was checked on a nested tree, non-root, `--cap-drop ALL`: **10
distinct pids traced, 10 `execve` calls**. So `-f` is unaffected.

**Task 13 should run trial containers with `--cap-drop ALL`.** Granting
`CAP_SYS_PTRACE` to a container running an agent under test is a gratuitous
weakening of the isolation that is the point of the sandbox: it would let that
process attach to any other process in the container.

The one caveat is a host Linux Security Module policy. The Docker Desktop VM
kernel used here has no Yama (`/proc/sys/kernel/yama/ptrace_scope` absent). A
host with Yama at scope 0 or 1 still permits descendant tracing; a host at scope
3 forbids `PTRACE_TRACEME` outright and no capability fixes it.
`tests/integration/test_images.py::test_strace_needs_no_added_capability`
asserts the `--cap-drop ALL` case so such a host fails loudly there rather than
silently inside a trial.

## Claude Code `--output-format json`: a free sample of the real envelope

Task 11 budgeted paid headless runs to record the output shape. It does not need
them for the envelope: an **unauthenticated** run emits a complete, well-formed
result record, exit code and all, at zero cost.

```bash
docker run --rm --entrypoint sh reprobe/claude-code:dev -lc \
    'claude -p "hi" --output-format json'
```

Field names observed on 2.1.289 (values elided):

```json
{"type":"result","subtype":"success","is_error":true,"num_turns":1,
 "result":"Not logged in · Please run /login","session_id":"...",
 "duration_ms":112,"duration_api_ms":0,"total_cost_usd":0,
 "usage":{"input_tokens":0,"output_tokens":0,
          "cache_creation_input_tokens":0,"cache_read_input_tokens":0,
          "service_tier":"standard"},
 "modelUsage":{},"permission_denials":[],"stop_reason":"stop_sequence",
 "terminal_reason":"api_error","api_error_status":null}
```

Worth noting for Task 11's parser:

- **`subtype` is `"success"` while `is_error` is `true`.** `subtype` describes
  the envelope, not the outcome. Branch on `is_error`, never on `subtype`.
- Token counts live under `usage` with `cache_creation_input_tokens` and
  `cache_read_input_tokens`, which map onto `budget.Cost`'s `cache_write_tokens`
  and `cache_read_tokens` respectively. The names do not match; the adapter
  translates.
- `total_cost_usd` is reported by the CLI, so the adapter can cross-check
  `reprobe.budget.price()` against the vendor's own number instead of trusting
  the price table blindly.
- This sample exercises the envelope only. The **streaming** `stream-json`
  event shapes (assistant messages, `tool_use` blocks) still need an
  authenticated run to record, which is the irreducible spend in Task 11.

## Flags

See the Phase-1 handoff for the verified Claude Code flag set. The two that bite:
`--output-format stream-json` **requires** `--verbose`, and `--max-turns` does
not exist in 2.1.232 or 2.1.289 — use `--max-budget-usd`, which maps onto
`Limits.max_usd_per_trial`.

Codex CLI flags are **not yet verified**. Task 12 must run `codex --help` and
`codex exec --help` and record the real output here. The `codex` binary is not
installed on the host, but `reprobe/codex-cli:dev` now contains 0.160.0, so the
verification can be done in the container without installing anything:

```bash
docker run --rm --entrypoint codex reprobe/codex-cli:dev --help
```
