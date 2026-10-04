"""R2: the Claude Code adapter.

Flags verified against `claude --help` on 2.1.289 inside
`reprobe/claude-code:dev`, not from documentation. Three of them bite:

* `--verbose` is **mandatory** with `--output-format stream-json`. Measured:
  `claude -p x --output-format stream-json` exits with
  "Error: When using --print, --output-format=stream-json requires --verbose".
  Omit it and every trial is a harness failure with empty stdout.
* `--max-turns` **does not exist**. An earlier draft of the plan used it, which
  would have made the CLI reject every invocation. `Limits.max_turns` has no
  equivalent here -- see `docs/agents.md` for how it is treated.
* `--max-budget-usd` is the per-trial cap, and maps onto
  `Limits.max_usd_per_trial`.

The stream's own failure reporting is the other trap: `subtype` describes the
envelope, not the outcome, so a failed run still says `"subtype":"success"`
alongside `"is_error":true`. Branch on `is_error`.
"""

from __future__ import annotations

import json
from typing import Any

from reprobe.agents.base import AgentSpec
from reprobe.budget import Cost, price
from reprobe.trace import Event

#: Input field names come from the vendor's shipped `sdk-tools.d.ts`
#: (`FileReadInput.file_path`, `BashInput.command`, ...), not from guesswork.
_PATH_KEYS = ("file_path", "path", "notebook_path")
_COMMAND_KEYS = ("command",)


class ClaudeCodeAdapter:
    id = "claude-code"
    image = "reprobe/claude-code:dev"

    #: Only the agent's own credentials. The sandbox passes nothing else, so
    #: anything missing here simply does not exist inside a trial.
    env_allowlist: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")

    def command(self, spec: AgentSpec, *, task: str, max_usd: float) -> list[str]:
        return [
            "claude",
            "-p",
            task,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            spec.model,
            "--max-budget-usd",
            str(max_usd),
            "--dangerously-skip-permissions",
            *spec.extra_args,
        ]

    def version_from(self, text: str) -> str:
        """`"2.1.289 (Claude Code)"` -> `"2.1.289"`.

        That string is what R11 pins a finding to, so anything that is not
        recognisably a version is returned whole rather than chopped to its
        first word -- a mangled pin is worse than an obviously wrong one.
        """
        stripped = text.strip()
        if not stripped:
            return "unknown"
        head = stripped.split()[0]
        return head if head[0].isdigit() else stripped

    # --- stream parsing ---------------------------------------------------

    def _records(self, text: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # a trial killed on its deadline truncates stdout
            if isinstance(record, dict):
                out.append(record)
        return out

    def error_from(self, text: str) -> str | None:
        """The failure the stream reports in-band, or None.

        A non-None answer means the agent never really ran, which is a harness
        error rather than a clean trial with no violations.
        """
        for record in self._records(text):
            if record.get("type") == "result" and record.get("is_error"):
                result = record.get("result")
                return str(result) if result else "agent reported an error"
        return None

    def parse_stdout(self, text: str, *, model: str = "") -> tuple[list[Event], Cost]:
        events: list[Event] = []
        cost = Cost.zero()
        # The stream carries no timestamps on most records, so order is the
        # only temporal information there is. A monotonic counter keeps the
        # trace sortable without inventing wall-clock times that would collide
        # with the other observers' real ones.
        for index, record in enumerate(self._records(text)):
            ts = float(index)
            kind = record.get("type")
            if kind == "system":
                events.append(
                    Event(
                        ts=ts,
                        kind="agent_message",
                        source="agent",
                        attrs={
                            "subtype": record.get("subtype", ""),
                            "model": record.get("model", ""),
                            "tools": record.get("tools", []),
                        },
                    )
                )
            elif kind == "assistant":
                events.extend(self._from_assistant(record, ts))
            elif kind == "user":
                events.extend(self._from_user(record, ts))
            elif kind == "result":
                cost = self._cost_from(record, model)
        return events, cost

    def _from_assistant(self, record: dict[str, Any], ts: float) -> list[Event]:
        events: list[Event] = []
        message = record.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                events.append(
                    Event(
                        ts=ts,
                        kind="agent_message",
                        source="agent",
                        attrs={"text": block.get("text", "")},
                    )
                )
            elif block.get("type") == "tool_use":
                events.append(self._tool_call(block, ts))
        return events

    def _tool_call(self, block: dict[str, Any], ts: float) -> Event:
        tool_input = block.get("input") or {}
        attrs: dict[str, Any] = {
            "name": block.get("name", "?"),
            "id": block.get("id", ""),
            "input": tool_input,
        }
        # Lifted to the top level so the checks do not each have to know which
        # key a given tool uses for its target.
        for key in _PATH_KEYS:
            if isinstance(tool_input, dict) and key in tool_input:
                attrs["path"] = tool_input[key]
                break
        for key in _COMMAND_KEYS:
            if isinstance(tool_input, dict) and key in tool_input:
                attrs["command"] = tool_input[key]
                break
        return Event(ts=ts, kind="tool_call", source="agent", attrs=attrs)

    def _from_user(self, record: dict[str, Any], ts: float) -> list[Event]:
        events: list[Event] = []
        message = record.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            # `agent_message` with the payload in `text`, not a `tool_result`
            # kind of its own. A canary the agent read via Bash shows up here
            # and nowhere else -- the filesystem never changed -- so it has to
            # land in the same text channel the checks already scan. A separate
            # kind would mean a check that forgot to scan it misses the
            # exfiltration, which is a false negative. `channel` keeps the two
            # distinguishable for a human reading a finding.
            events.append(
                Event(
                    ts=ts,
                    kind="agent_message",
                    source="agent",
                    attrs={
                        "channel": "tool_result",
                        "tool_use_id": block.get("tool_use_id", ""),
                        "text": content if isinstance(content, str) else json.dumps(content),
                        "is_error": bool(block.get("is_error", False)),
                    },
                )
            )
        return events

    def _cost_from(self, record: dict[str, Any], model: str) -> Cost:
        """Translate the CLI's usage keys onto `Cost`'s.

        They do not match: the CLI says `cache_creation_input_tokens` and
        `cache_read_input_tokens`, `Cost` says `cache_write_tokens` and
        `cache_read_tokens`.
        """
        usage = record.get("usage") or {}

        def count(key: str) -> int:
            value = usage.get(key, 0)
            return int(value) if isinstance(value, (int, float)) else 0

        input_tokens = count("input_tokens")
        output_tokens = count("output_tokens")
        cache_write = count("cache_creation_input_tokens")
        cache_read = count("cache_read_input_tokens")
        try:
            usd = price(model, input_tokens, output_tokens, cache_read, cache_write)
        except KeyError:
            # An unpriced or pinned-but-unknown model must not lose the token
            # counts: the run store keeps them so it can be re-priced later.
            usd = 0.0
        return Cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            usd=usd,
        )
