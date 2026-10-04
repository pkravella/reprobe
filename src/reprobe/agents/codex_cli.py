"""R2: the Codex CLI adapter.

Everything here was verified against codex 0.160.0 in `reprobe/codex-cli:dev`
by running it, including one authenticated trial. Four things differ from
Claude Code and none of them are guessable:

* **`OPENAI_API_KEY` in the environment is not enough.** With the key present,
  `codex exec` still returns "401 Unauthorized: Missing bearer or basic
  authentication in header". The key must be fed through
  `codex login --with-api-key` on **stdin** first, which writes credentials
  into `CODEX_HOME`. Hence `login_command()`.
* **Codex's own sandbox cannot nest inside a container.** With
  `--sandbox read-only` every shell command fails with "bwrap: No permissions
  to create a new namespace", because Codex sandboxes with bubblewrap and that
  needs user namespaces. The agent can then do nothing at all, so no violation
  could ever be observed -- silently useless rather than loudly broken.
  `--dangerously-bypass-approvals-and-sandbox` is documented as "intended
  solely for running in environments that are externally sandboxed", which is
  exactly what the trial container is. Verified: `exit_code: 0`.
* **`input_tokens` is a total, not the uncached remainder.** Measured over two
  real runs, `input_tokens == cached_input_tokens + cache_write_input_tokens +
  a small remainder`. Passing all three to `price()` bills ~25k tokens twice
  per trial.
* **There is no budget flag.** `codex exec` has no `--max-budget-usd`
  equivalent, so the per-trial cap is the ledger's job alone.

The event stream shares nothing with Claude Code's beyond being JSONL: flat
`{"type": "<dotted.name>"}` envelopes, with `item.started` / `item.completed`
wrapping a nested `item` that carries its own `type`.
"""

from __future__ import annotations

import json
from typing import Any

from reprobe.agents.base import AgentSpec
from reprobe.budget import Cost, price
from reprobe.trace import Event

#: Terminal events. Exactly one should appear; neither means the run was cut off.
_TERMINAL_OK = "turn.completed"
_TERMINAL_FAIL = "turn.failed"


class CodexCliAdapter:
    id = "codex-cli"
    image = "reprobe/codex-cli:dev"
    env_allowlist: tuple[str, ...] = ("OPENAI_API_KEY", "OPENAI_BASE_URL")

    #: `codex exec` has no per-trial dollar cap, so the budget ledger is the
    #: only thing enforcing one. Declared rather than silently assumed.
    enforces_max_usd = False

    def login_command(self) -> list[str] | None:
        """Pipe the key into `codex login` on stdin, before `exec`.

        The key never appears in argv: it would otherwise be visible in the
        process list and in the trial's own strace log, which Reprobe stores.
        """
        return ["sh", "-lc", "printenv OPENAI_API_KEY | codex login --with-api-key"]

    def command(self, spec: AgentSpec, *, task: str, max_usd: float) -> list[str]:
        # max_usd is accepted for protocol compatibility and deliberately
        # unused: see `enforces_max_usd`.
        return [
            "codex",
            "exec",
            "--json",
            "--model",
            spec.model,
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ephemeral",
            task,
            *spec.extra_args,
        ]

    def version_from(self, text: str) -> str:
        """`"codex-cli 0.160.0"` -> `"0.160.0"`.

        The version is the *second* token here, where Claude Code's is the
        first, so this cannot be shared between the two adapters.
        """
        parts = text.strip().split()
        for part in parts:
            if part and part[0].isdigit():
                return part
        return parts[0] if parts else "unknown"

    # --- stream parsing ---------------------------------------------------

    def _records(self, text: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            # Codex logs to stderr, but tolerate a merged stream rather than
            # letting a stray log line end the trial.
            if not stripped or not stripped.startswith("{"):
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                out.append(record)
        return out

    def error_from(self, text: str) -> str | None:
        """The failure the stream reports, or None.

        A stream with no terminal event is also an error: a trial killed on its
        deadline never emits `turn.completed`, and reading that as success
        turns a timeout into a clean trial with no violations.
        """
        saw_terminal_ok = False
        for record in self._records(text):
            kind = record.get("type")
            if kind == _TERMINAL_FAIL:
                error = record.get("error") or {}
                message = error.get("message") if isinstance(error, dict) else None
                return str(message) if message else "codex reported a failed turn"
            if kind == _TERMINAL_OK:
                saw_terminal_ok = True
        if not saw_terminal_ok:
            return "codex produced no terminal event; the run was cut off"
        return None

    def parse_stdout(self, text: str, *, model: str = "") -> tuple[list[Event], Cost]:
        events: list[Event] = []
        cost = Cost.zero()
        # No timestamps in the stream, so position is the only ordering there
        # is. Small integers stay visibly synthetic next to the real epoch
        # times from strace and the gateway.
        for index, record in enumerate(self._records(text)):
            kind = record.get("type")
            ts = float(index)
            if kind == "item.completed":
                # `item.started` is deliberately ignored: an item is reported
                # twice and emitting both would double every command in the
                # trace and in the tool sequence.
                event = self._from_item(record.get("item") or {}, ts)
                if event is not None:
                    events.append(event)
            elif kind == _TERMINAL_OK:
                cost = self._cost_from(record.get("usage") or {}, model)
            elif kind == "error":
                message = record.get("message")
                if message:
                    events.append(
                        Event(
                            ts=ts,
                            kind="agent_message",
                            source="agent",
                            attrs={"channel": "error", "text": str(message)},
                        )
                    )
        return events, cost

    def _from_item(self, item: dict[str, Any], ts: float) -> Event | None:
        item_type = item.get("type")
        if item_type == "agent_message":
            return Event(
                ts=ts,
                kind="agent_message",
                source="agent",
                attrs={"text": str(item.get("text", "")), "id": item.get("id", "")},
            )
        if item_type == "command_execution":
            return Event(
                ts=ts,
                kind="tool_call",
                source="agent",
                attrs={
                    "name": "command_execution",
                    "id": item.get("id", ""),
                    "command": str(item.get("command", "")),
                    # Lands in the trace so a canary read by a shell command is
                    # visible: it appears nowhere else.
                    "output": str(item.get("aggregated_output", "")),
                    "exit_code": item.get("exit_code"),
                    "status": item.get("status", ""),
                },
            )
        if item_type == "error":
            return Event(
                ts=ts,
                kind="agent_message",
                source="agent",
                attrs={"channel": "error", "text": str(item.get("message", ""))},
            )
        # file_change, mcp_tool_call, web_search, reasoning, patch_apply and
        # todo_list exist in the binary's item-type set but have not been seen
        # in a recording, so they are not mapped on a guess. The raw stream is
        # stored alongside the trace, so nothing is lost.
        return None

    def _cost_from(self, usage: dict[str, Any], model: str) -> Cost:
        def count(key: str) -> int:
            value = usage.get(key, 0)
            return int(value) if isinstance(value, (int, float)) else 0

        total_input = count("input_tokens")
        cache_read = count("cached_input_tokens")
        cache_write = count("cache_write_input_tokens")
        # input_tokens is the TOTAL: subtract the cache figures out of it or
        # the same tokens are billed two and three times over.
        fresh_input = max(total_input - cache_read - cache_write, 0)
        output_tokens = count("output_tokens")
        try:
            usd = price(model, fresh_input, output_tokens, cache_read, cache_write)
        except KeyError:
            usd = 0.0
        return Cost(
            input_tokens=fresh_input,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            usd=usd,
        )
