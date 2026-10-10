"""Claude Code adapter (R2).

`tests/data/claude-stream-recorded.jsonl` is a **verbatim recording** of a real
authenticated trial on claude-haiku-4-5 in `reprobe/claude-code:dev` (2.1.289).
The agent was asked to use the Read tool and then the Bash tool, so the stream
contains the real `tool_use` and `tool_result` shapes, `thinking` blocks, the
`system/thinking_tokens` events, and a `result` envelope with real token counts.

Only per-run uuids and the timestamp were stabilised so the file does not
churn, and the `thinking.signature` blobs were truncated -- each is multiple KB
of opaque model artifact and no assertion touches them.

This replaces an earlier constructed fixture. Worth recording that the
construction turned out to be **accurate**: it was built from the recorded
assistant envelope with input field names taken from the vendor's shipped
`sdk-tools.d.ts`, and the real `tool_use` / `tool_result` shapes match it. The
real stream added two things the construction could not have known about --
`thinking` blocks and `system/thinking_tokens` events.
"""

from pathlib import Path

import pytest

from reprobe.agents import available, get_adapter
from reprobe.agents.base import AgentSpec
from reprobe.agents.claude_code import ClaudeCodeAdapter

STREAM = Path("tests/data/claude-stream-recorded.jsonl").read_text()
CANARY = "RPRB_CANARY_" + "A" * 32


def _spec(**kw) -> AgentSpec:
    return AgentSpec(**{"id": "claude-code", "model": "claude-haiku-4-5", **kw})


# --- registry -------------------------------------------------------------


def test_the_registry_exposes_the_claude_code_adapter():
    assert isinstance(get_adapter("claude-code"), ClaudeCodeAdapter)


def test_an_unknown_agent_id_is_refused_with_the_known_ones_listed():
    with pytest.raises(KeyError, match="claude-code"):
        get_adapter("gpt-9")


def test_available_lists_the_registered_ids():
    assert "claude-code" in available()


# --- the command line -----------------------------------------------------


def test_command_uses_the_flags_that_actually_exist():
    # Verified against `claude --help` on 2.1.289 in the image.
    cmd = ClaudeCodeAdapter().command(_spec(), task="do the thing", max_usd=0.5)
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "do the thing" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-4-5"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.5"
    assert "--dangerously-skip-permissions" in cmd


def test_command_passes_verbose_because_stream_json_requires_it():
    # Measured: `claude -p x --output-format stream-json` exits with
    # "Error: When using --print, --output-format=stream-json requires
    # --verbose". Without this flag every trial is a harness failure.
    assert "--verbose" in ClaudeCodeAdapter().command(_spec(), task="t", max_usd=1.0)


def test_command_never_passes_max_turns():
    # --max-turns does not exist in any verified version. An earlier draft of
    # the plan used it, which would have made the CLI reject every invocation.
    assert "--max-turns" not in ClaudeCodeAdapter().command(_spec(), task="t", max_usd=1.0)


def test_extra_args_are_appended():
    cmd = ClaudeCodeAdapter().command(_spec(extra_args=["--effort", "low"]), task="t", max_usd=1.0)
    assert cmd[-2:] == ["--effort", "low"]


def test_the_task_is_passed_as_one_argument_not_shell_quoted():
    # The command goes to Docker as an argv list, so a task containing quotes
    # or newlines must survive untouched rather than being re-escaped.
    task = 'fix the "bug"\nin src/widget.js; echo hi'
    cmd = ClaudeCodeAdapter().command(_spec(), task=task, max_usd=1.0)
    assert task in cmd


def test_env_allowlist_is_minimal_and_explicit():
    allow = ClaudeCodeAdapter().env_allowlist
    assert "ANTHROPIC_API_KEY" in allow
    assert all(name.startswith("ANTHROPIC_") for name in allow), allow
    assert "PATH" not in allow and "HOME" not in allow


def test_required_env_is_the_api_key_and_nothing_optional():
    """What an exported suite refuses to run without. `ANTHROPIC_BASE_URL` is
    allowed through but optional, so it must not be required -- and the draft's
    `env_allowlist[:1]` got this right only by tuple order."""
    adapter = ClaudeCodeAdapter()
    assert adapter.required_env == ("ANTHROPIC_API_KEY",)
    assert set(adapter.required_env) <= set(adapter.env_allowlist)


def test_the_adapter_names_its_image():
    assert ClaudeCodeAdapter().image == "reprobe/claude-code:dev"


# --- parsing the recorded envelopes ---------------------------------------


def test_the_recorded_fixture_is_the_real_envelope():
    # Guards provenance: if these disappear the fixture stopped being a
    # recording and the parser stops being tested against reality.
    assert '"claude_code_version":"2.1.289"' in STREAM
    assert '"type":"system"' in STREAM and '"subtype":"init"' in STREAM
    assert '"apiKeySource":"ANTHROPIC_API_KEY"' in STREAM, "not an authenticated run"
    assert '"type":"tool_use"' in STREAM and '"type":"tool_result"' in STREAM
    assert '"type":"thinking"' in STREAM


def test_the_system_init_line_yields_an_agent_message_event():
    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    init = [e for e in events if e.attrs.get("subtype") == "init"]
    assert len(init) == 1
    assert init[0].kind == "agent_message"


def test_assistant_text_becomes_an_agent_message():
    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    texts = [e.attrs.get("text", "") for e in events if e.kind == "agent_message"]
    assert any("The package name is **widget**." in t for t in texts)


def test_cost_comes_from_the_result_usage_block():
    _, cost = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    assert cost.input_tokens == 18
    assert cost.output_tokens == 251
    assert cost.cache_read_tokens == 34546
    assert cost.cache_write_tokens == 7104
    assert cost.usd > 0


def test_input_tokens_exclude_cached_tokens_for_this_vendor():
    # Measured on the real trial: input_tokens 18 alongside
    # cache_read_input_tokens 34546. So the four figures are additive and pass
    # straight through. Codex is the opposite -- its input_tokens is the total
    # and the cache figures are subsets -- which is why the two adapters
    # cannot share this mapping.
    _, cost = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    assert cost.input_tokens < cost.cache_read_tokens


def test_the_price_table_reproduces_the_vendors_own_reported_cost():
    # The strongest check available on the price table: the CLI reports
    # total_cost_usd itself, so our arithmetic can be compared to it rather
    # than to a published rate card someone transcribed.
    import json

    line = next(row for row in STREAM.splitlines() if '"type":"result"' in row)
    result = json.loads(line)
    _, cost = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    assert cost.usd == pytest.approx(result["total_cost_usd"], rel=1e-6)


def test_usage_key_names_are_translated_not_assumed():
    # The CLI reports cache_creation_input_tokens / cache_read_input_tokens.
    # reprobe.budget.Cost calls them cache_write_tokens / cache_read_tokens.
    # The names do not match, so the adapter must translate.
    stream = (
        '{"type":"result","subtype":"success","is_error":false,"num_turns":1,'
        '"usage":{"input_tokens":10,"output_tokens":20,'
        '"cache_creation_input_tokens":30,"cache_read_input_tokens":40}}\n'
    )
    _, cost = ClaudeCodeAdapter().parse_stdout(stream, model="claude-haiku-4-5")
    assert (cost.input_tokens, cost.output_tokens) == (10, 20)
    assert (cost.cache_write_tokens, cost.cache_read_tokens) == (30, 40)


def test_an_unpriced_model_does_not_explode_the_parse():
    # price() raises KeyError on an unknown model. A trial must still return its
    # events and token counts so the run can be re-priced later.
    _, cost = ClaudeCodeAdapter().parse_stdout(STREAM, model="not-a-real-model")
    assert cost.input_tokens == 18
    assert cost.cache_read_tokens == 34546
    assert cost.usd == 0.0


# --- the failure the stream reports in-band -------------------------------


def test_the_recorded_successful_stream_reports_no_error():
    assert ClaudeCodeAdapter().error_from(STREAM) is None


def test_an_api_error_message_is_surfaced_not_silently_parsed():
    # Recorded verbatim from an unauthenticated run: `subtype` is "success"
    # while `is_error` is true, so branching on subtype reads a total failure
    # as a clean trial that did nothing.
    failed = (
        '{"type":"result","subtype":"success","is_error":true,"num_turns":1,'
        '"usage":{"input_tokens":0,"output_tokens":0},'
        '"result":"Not logged in \u00b7 Please run /login"}\n'
    )
    assert "Not logged in" in str(ClaudeCodeAdapter().error_from(failed))


def test_a_successful_stream_reports_no_error():
    stream = (
        '{"type":"result","subtype":"success","is_error":false,"num_turns":2,'
        '"usage":{"input_tokens":1,"output_tokens":1},"result":"done"}\n'
    )
    assert ClaudeCodeAdapter().error_from(stream) is None


def test_subtype_success_with_is_error_true_is_still_an_error():
    stream = (
        '{"type":"result","subtype":"success","is_error":true,"num_turns":1,'
        '"usage":{"input_tokens":0,"output_tokens":0},"result":"boom"}\n'
    )
    assert ClaudeCodeAdapter().error_from(stream) == "boom"


# --- tool calls (synthetic fixture) ---------------------------------------


def test_tool_use_blocks_become_tool_call_events():
    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    calls = [e for e in events if e.kind == "tool_call"]
    assert [e.attrs["name"] for e in calls] == ["Read", "Bash"]


def test_a_read_tool_call_records_its_path():
    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    read = next(e for e in events if e.attrs.get("name") == "Read")
    # Field name from the vendor's sdk-tools.d.ts: FileReadInput.file_path.
    assert read.attrs["path"] == "/workspace/README.md"


def test_a_bash_tool_call_records_its_command():
    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    bash = next(e for e in events if e.attrs.get("name") == "Bash")
    assert bash.attrs["command"] == "cat /workspace/package.json"


def test_tool_results_land_in_the_text_channel_the_checks_scan():
    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    results = [e for e in events if e.attrs.get("channel") == "tool_result"]
    assert len(results) == 2
    assert all(e.kind == "agent_message" for e in results)
    # The real Read result is line-numbered: "1\t# Widget\n2\t\n3\t...".
    assert any("A widget library." in e.attrs["text"] for e in results)
    assert any('"name":"widget"' in e.attrs["text"] for e in results)


@pytest.mark.parametrize(
    "content",
    [
        f"API_KEY={CANARY}",
        # The real Read tool returns line-numbered content, so a canary in a
        # read file arrives with a "1\t" prefix.
        f"1\tAPI_KEY={CANARY}\n2\t",
    ],
)
def test_a_canary_in_tool_output_is_found_by_scanning_agent_text_alone(content):
    # The property Task 14 depends on: one scan over agent_message text catches
    # both what the model said and what a tool handed it. The filesystem never
    # changes when the agent just reads, so this is the only evidence there is.
    import json as _json

    from reprobe.canary import Canary, find_canaries
    from reprobe.scenario import CanarySpec

    stream = (
        _json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": content}]
                },
            }
        )
        + "\n"
    )
    canary = Canary(
        id="api_key", value=CANARY, spec=CanarySpec(id="api_key", kind="file", path=".env")
    )
    events, _ = ClaudeCodeAdapter().parse_stdout(stream, model="claude-haiku-4-5")
    text = "\n".join(e.attrs.get("text", "") for e in events if e.kind == "agent_message")
    assert find_canaries(text, [canary]) == ["api_key"]


def test_tool_sequence_is_recoverable_from_the_trace():
    from reprobe.trace import Trace

    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    assert Trace(trial_id="t", events=events).tool_sequence() == ["Read", "Bash"]


# --- robustness -----------------------------------------------------------


def test_events_are_ordered_and_attributed_to_the_agent():
    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    assert {e.source for e in events} == {"agent"}
    assert [e.ts for e in events] == sorted(e.ts for e in events)


def test_events_round_trip_through_the_trace_jsonl():
    from reprobe.trace import Trace

    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    trace = Trace(trial_id="t", events=events)
    assert Trace.from_jsonl(trace.to_jsonl(), trial_id="t") == trace


def test_a_torn_final_line_is_tolerated():
    # A trial killed on its deadline truncates stdout mid-line.
    events, cost = ClaudeCodeAdapter().parse_stdout(
        STREAM + '{"type":"assistant","message":{"cont', model="claude-haiku-4-5"
    )
    assert events
    assert cost.input_tokens >= 0


def test_blank_and_non_object_lines_are_skipped():
    events, _ = ClaudeCodeAdapter().parse_stdout(
        "\n[]\n" + STREAM + '"a string"\n', model="claude-haiku-4-5"
    )
    assert events


def test_empty_stdout_yields_nothing_rather_than_raising():
    events, cost = ClaudeCodeAdapter().parse_stdout("", model="claude-haiku-4-5")
    assert events == []
    assert cost.usd == 0.0


# --- version --------------------------------------------------------------


def test_version_from_reads_the_recorded_version_string():
    # The image writes `claude --version` to a file at build time, which is
    # what a finding is pinned to.
    assert ClaudeCodeAdapter().version_from("2.1.289 (Claude Code)") == "2.1.289"


def test_version_from_tolerates_junk():
    assert ClaudeCodeAdapter().version_from("") == "unknown"
    assert ClaudeCodeAdapter().version_from("not a version") == "not a version"


def test_malformed_content_blocks_are_skipped():
    # The stream is the only thing standing between adversarial output and the
    # parser, so a block that is not a dict, or a user message carrying
    # something other than a tool_result, must not take the trial down.
    stream = (
        '{"type":"assistant","message":{"content":["a bare string",null,42,'
        '{"type":"thinking","thinking":"hmm"},{"type":"text","text":"ok"}]}}\n'
        '{"type":"user","message":{"content":["junk",{"type":"text","text":"not a result"}]}}\n'
    )
    events, _ = ClaudeCodeAdapter().parse_stdout(stream, model="claude-haiku-4-5")
    assert [e.attrs.get("text") for e in events] == ["hmm", "ok"]


def test_thinking_text_reaches_the_channel_the_checks_scan():
    # If the agent reasons about a canary value out loud, that is evidence. The
    # recorded thinking blocks have empty text (the content is in an opaque
    # signature), but a model emitting visible reasoning must not have it
    # dropped.
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"thinking",'
        '"thinking":"the key is ' + CANARY + '","signature":"opaque"}]}}\n'
    )
    events, _ = ClaudeCodeAdapter().parse_stdout(stream, model="claude-haiku-4-5")
    assert [e.attrs["channel"] for e in events] == ["thinking"]
    assert CANARY in events[0].attrs["text"]


def test_the_opaque_thinking_signature_is_not_carried_into_the_trace():
    # Multiple KB per block, of no use to any check, and it would bloat every
    # stored trace.
    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    assert not any("signature" in e.attrs for e in events)


def test_empty_thinking_blocks_produce_nothing():
    # Which is what the real recording contains: the reasoning is encrypted in
    # the signature, so there is no text to scan.
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"thinking",'
        '"thinking":"","signature":"opaque"}]}}\n'
    )
    assert ClaudeCodeAdapter().parse_stdout(stream, model="claude-haiku-4-5")[0] == []


def test_the_system_thinking_tokens_events_are_recorded_without_breaking_anything():
    # Present twice in the real stream; the plan's sample had no such event.
    assert '"subtype":"thinking_tokens"' in STREAM
    events, _ = ClaudeCodeAdapter().parse_stdout(STREAM, model="claude-haiku-4-5")
    subtypes = {e.attrs.get("subtype") for e in events}
    assert "thinking_tokens" in subtypes
