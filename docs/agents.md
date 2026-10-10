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
docker run --rm node:26-slim sh -lc 'npm view @anthropic-ai/claude-code version'   # 2.1.289
docker run --rm node:26-slim sh -lc 'npm view @openai/codex version'              # 0.160.0
```

Both package names in the plan were correct. `@openai/codex` additionally
publishes per-platform dist-tags (`linux-arm64`, `darwin-x64`, …); installing
plain `@openai/codex` resolves the right platform build through optional
dependencies, which is what the image does.

## Why the base image is a `node:<major>-bookworm-slim`

Claude Code declares `engines: {node: ">=22.0.0"}`. Debian bookworm's own
`nodejs` package is **18.20.4**, so the plan's `debian:bookworm-slim` base would
run the agent on an engine it does not claim to support.

Measured, so the record is accurate: on Node 18 `npm install -g` **succeeds**
with only an `npm WARN EBADENGINE` warning, and both `claude --help` and a full
`claude -p "hi" --output-format json` run behave **indistinguishably** from Node
22. This is therefore a latent risk, not an observed break. It is still the
wrong runtime to carry into the Phase-1 gate, which requires zero harness
failures across 100 trials — an unsupported engine is an avoidable variable, and
a `-bookworm-` tag keeps the image on Debian 12, so every apt package is
unchanged.

**The major version is not pinned in prose on purpose.** Dependabot bumps the
`FROM` line monthly, and
`tests/integration/test_images.py::test_base_image_satisfies_the_agent_cli_declared_node_engine`
asserts `node >= 22` -- the declared constraint -- rather than an exact
version, so a major bump does not produce a spurious failure. Two things to
keep when reviewing such a bump: the tag must stay `-bookworm-`, and the agent
CLIs must still run on it.

### Verification record

| Base | Verified | Result |
| --- | --- | --- |
| `node:22-bookworm-slim` | 2026-10-04 | Node 22.23.3; all images build; both CLIs run |
| `node:26-bookworm-slim` | 2026-10-04 | Node 26.10.0; see below |

`node:26-bookworm-slim` (Dependabot, merged) was checked before merge rather
than after:

- Still `Debian GNU/Linux 12 (bookworm)`, so no apt package changed. Node
  26.10.0, npm 11.19.1.
- All four images build.
- `claude-code`: version probe records `2.1.289 (Claude Code)`, `--help` works,
  and a full `claude -p "hi" --output-format json` run emits the complete
  result envelope -- the same shape as on Node 22.
- `codex`: version probe records `codex-cli 0.160.0`, and `codex exec --help`
  is **byte-identical** to the recording taken on Node 22 and on the host.
- The full docker integration suite passes (14 tests, the whole suite as it stood on
  2026-10-04; it is 52 at the time of writing).

Taking the bump early was deliberate: it changes the container id every
exported finding is pinned to (R11), and with no findings and no benchmark yet
recorded, that id is pinned to nothing. The same bump after the Phase-1 gate
would invalidate the gate's recorded digest and its cost numbers.

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

## Claude Code flags

Re-verified against `claude --help` **inside `reprobe/claude-code:dev` on
2.1.289**, 2026-10-04. Identical to the 2.1.232 findings.

| Flag | Status |
| --- | --- |
| `-p, --print` | exists; non-interactive |
| `--output-format <text\|json\|stream-json>` | exists, only with `--print` |
| `--verbose` | **required** with `stream-json` |
| `--model <model>` | exists; alias or full id |
| `--max-turns` | **DOES NOT EXIST** in 2.1.232 or 2.1.289 |
| `--max-budget-usd <amount>` | exists; the per-trial cap |
| `--dangerously-skip-permissions` | exists |
| `--permission-mode` | exists |
| `--effort <low..max>` | exists; unused |
| `--include-partial-messages` | exists; unused |

The `--verbose` requirement is not advisory. Measured:

```
$ claude -p "x" --output-format stream-json
Error: When using --print, --output-format=stream-json requires --verbose
```

Omit it and every trial is a harness failure with empty stdout.

So the verified invocation is:

```
claude -p "<task>" --output-format stream-json --verbose \
       --model <model> --max-budget-usd <cap> --dangerously-skip-permissions
```

### `Limits.max_turns` is advisory for this agent — and says so

Claude Code has no turn cap, so the adapter cannot honour `max_turns`. It is
**not silently ignored**: the adapter simply never claims to support it, and
**Task 15's orchestrator must warn when a scenario sets `max_turns` and the
selected adapter cannot enforce it**. A declared limit that quietly does
nothing is worse than no limit, because a scenario author believes a bound
exists. `max_usd_per_trial` is the cap that is actually enforced, by
`--max-budget-usd` and independently by the budget ledger.

## The `stream-json` shape

### Recorded from a real trial

`tests/data/claude-stream-recorded.jsonl` is a verbatim recording of an
authenticated trial on `claude-haiku-4-5`, asked to use the Read tool and then
the Bash tool. It contains the real `tool_use` and `tool_result` shapes,
`thinking` blocks, `system/thinking_tokens` events and a `result` envelope with
real token counts. Only per-run uuids and the timestamp are stabilised, and the
multi-KB opaque `thinking.signature` blobs are truncated.

Note that three of the four record types cost **nothing** to capture — an
unauthenticated run still emits the real `system/init`, `assistant` and
`result` envelopes:

```bash
docker run --rm --entrypoint claude reprobe/claude-code:dev \
    -p "hi" --output-format stream-json --verbose
```

Only `tool_use` / `tool_result` need a real turn. An earlier fixture
constructed those from the recorded envelope plus the vendor's shipped
`sdk-tools.d.ts`, and the real recording confirmed that construction was
**accurate** — the shapes matched. It could not have predicted `thinking`
blocks or `system/thinking_tokens` events, which is why the recording replaced
it.

Verified block shapes:

```json
{"type":"tool_use","id":"toolu_...","name":"Read",
 "input":{"file_path":"/workspace/README.md"},"caller":{"type":"direct"}}

{"type":"tool_result","tool_use_id":"toolu_...",
 "content":"1\t# Widget\n2\t\n3\tA widget library.\n4\t"}

{"type":"thinking","thinking":"","signature":"<multi-KB opaque blob>"}
```

Two things to know. **The Read tool returns line-numbered content** (`1\t`,
`2\t`, …), so a canary in a read file arrives with a numeric prefix —
`find_canaries` handles it because it tolerates whitespace splitting, and
there is a test for exactly that form. And **`thinking.thinking` was empty** in
the recording, with the reasoning encrypted in the signature; the adapter still
routes any non-empty thinking text into the channel the checks scan, since a
model reasoning about a canary out loud is evidence. The signature is dropped.

Tool input field names, confirmed against `sdk-tools.d.ts` and the recording:

| Tool | Input fields |
| --- | --- |
| `Read` | `file_path`, `offset?`, `limit?` |
| `Write` | `file_path`, `content` |
| `Edit` | `file_path`, `old_string`, `new_string`, `replace_all?` |
| `Bash` | `command`, `timeout?`, `description?`, `run_in_background?` |

### `input_tokens` excludes cached tokens — the opposite of Codex

Real usage from the trial:

```json
{"input_tokens":18,"cache_creation_input_tokens":7104,
 "cache_read_input_tokens":34546,"output_tokens":251,
 "output_tokens_details":{"thinking_tokens":116}}
```

`input_tokens` is **18** alongside 34546 cached reads, so the four figures are
**additive** and pass straight through to `price()`. Codex reports the
opposite — its `input_tokens` is the total with the cache figures as subsets —
which is why the two adapters cannot share this mapping.

### The price table checks out against the vendor's own number

The CLI reports `total_cost_usd` itself, so the local price table can be
compared to it rather than to a transcribed rate card. For the trial above:

```
our price()      $0.0136076
vendor reported  $0.0136076
```

Exact to seven decimal places, which validates both the `claude-haiku-4-5`
rates and the cache-token mapping. There is a regression test asserting the
two agree.

### Two traps in the result envelope

- **`subtype` is `"success"` while `is_error` is `true`.** `subtype` describes
  the envelope, not the outcome. Branching on it makes a real auth failure
  report as a clean trial that happened to do nothing, which is exactly the
  harness-error-as-pass confusion this project treats as load-bearing. Branch
  on `is_error`, which is what `ClaudeCodeAdapter.error_from` does.
- **The usage keys do not match `reprobe.budget.Cost`.** The CLI reports
  `cache_creation_input_tokens` and `cache_read_input_tokens`; `Cost` calls
  them `cache_write_tokens` and `cache_read_tokens`. The adapter translates.
  Token counts are kept even when `price()` does not know the model, so an
  archived run can be re-priced.

`total_cost_usd` is also reported, so a trial can cross-check the local price
table against the vendor's own number.

## Codex CLI

Verified on **0.160.0** in `reprobe/codex-cli:dev`, including one real
authenticated trial. All four flags the plan guessed at do exist (`--json`,
`--model`, `--sandbox`, `--skip-git-repo-check`), but three things about this
CLI are not guessable and each one breaks a trial silently.

### `OPENAI_API_KEY` in the environment is not enough

With the key present in the container, `codex exec` still fails:

```
{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized:
 Missing bearer or basic authentication in header, ..."}}
```

The key has to be piped into a login step first, which writes credentials into
`CODEX_HOME`:

```bash
printenv OPENAI_API_KEY | codex login --with-api-key
```

Then `codex login status` reports `Logged in using an API key`. Claude Code
reads its key straight from the environment and needs nothing, which is why
`AgentAdapter.login_command()` exists rather than the sandbox hard-coding one
behaviour. **The key must arrive on stdin, never in argv** — argv is visible in
the process list and in the trial's own strace log, which Reprobe stores.

### Codex's own sandbox cannot nest inside a container

With `--sandbox read-only` (or any bubblewrap-backed mode) every shell command
fails:

```json
{"type":"item.completed","item":{"type":"command_execution",
 "command":"/bin/bash -lc 'cat README.md'",
 "aggregated_output":"bwrap: No permissions to create a new namespace, likely
  because the kernel does not allow non-privileged user namespaces...",
 "exit_code":1,"status":"failed"}}
```

Codex sandboxes with bubblewrap, which needs user namespaces the container does
not grant. The agent can then do **nothing**, so no violation could ever be
observed — a trial that is silently useless rather than loudly broken, which is
the worst failure mode available.

`--dangerously-bypass-approvals-and-sandbox` is documented as "intended solely
for running in environments that are externally sandboxed", which is precisely
what a trial container is. Verified working: same command, `exit_code: 0`,
output captured. The adapter also passes `--ignore-user-config` and
`--ephemeral` so no host config bleeds in and no session state persists between
trials.

### `input_tokens` is a total, and double-counting it inflates every cost

`turn.completed` reports usage with its own key names, which match neither
Claude Code's nor `reprobe.budget.Cost`'s:

| Codex | Claude Code | `Cost` |
| --- | --- | --- |
| `input_tokens` | `input_tokens` | `input_tokens` |
| `cached_input_tokens` | `cache_read_input_tokens` | `cache_read_tokens` |
| `cache_write_input_tokens` | `cache_creation_input_tokens` | `cache_write_tokens` |
| `output_tokens` | `output_tokens` | `output_tokens` |
| `reasoning_output_tokens` | — | — |

And the arithmetic differs. Measured over two real runs:

| run | `input_tokens` | `cached` | `cache_write` | cached + write | remainder |
| --- | --- | --- | --- | --- | --- |
| A | 24951 | 12409 | 12536 | 24945 | 6 |
| B | 24916 | 12399 | 12511 | 24910 | 6 |

So `input_tokens` is the **total**, with the cache figures breaking it down —
unlike Anthropic, which reports cache reads *separately* from `input_tokens`.
Passing all three straight to `price()` bills roughly 25k tokens two and three
times over on every trial, which trips a budget cap early and makes every cost
estimate wrong. The adapter subtracts.

`reasoning_output_tokens` was 0 in both runs, so whether it is additive to
`output_tokens` or already included is **not** established. It is currently
ignored; a run that actually uses reasoning tokens would settle it.

### No budget flag

`codex exec` has no `--max-budget-usd` equivalent, so for this agent the
per-trial cap is enforced by the budget ledger alone. The adapter declares this
as `enforces_max_usd = False` rather than leaving it implicit — Claude Code sets
it True, where `--max-budget-usd` gives a second line of defence.

### `codex exec` reads stdin

Stderr shows `Reading additional input from stdin...` on every run. **Stdin must
be closed** or the process waits. Task 13 owes `stdin_open=False`.

### Event stream

Pure JSONL on stdout; the `ERROR` log lines go to stderr. Flat
`{"type":"<dotted.name>"}` envelopes, sharing nothing with Claude Code's shape:

```
thread.started   {"thread_id": ...}
turn.started
item.started     {"item": {"id", "type", ...}}
item.completed   {"item": {"id", "type", ...}}
turn.completed   {"usage": {...}}
turn.failed      {"error": {"message"}}
error            {"message"}
```

An item is reported **twice**, as `item.started` then `item.completed`, so only
the completed form is emitted or every command doubles in the trace.

Item payloads, from recordings:

| item type | fields |
| --- | --- |
| `agent_message` | `id`, `type`, `text` |
| `command_execution` | `id`, `type`, `command`, `aggregated_output`, `exit_code`, `status` |
| `error` | `id`, `type`, `message` |

`file_change`, `mcp_tool_call`, `web_search`, `reasoning`, `patch_apply` and
`todo_list` appear in the binary's item-type table but have not been seen in a
recording, so the adapter leaves them unmapped rather than guessing. The raw
stream is stored beside the trace, so a later recording can map them without
having lost anything.
