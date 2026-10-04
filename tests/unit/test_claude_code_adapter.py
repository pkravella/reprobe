"""Claude Code adapter (R2).

Two fixtures, and the difference between them matters:

* `claude-stream-recorded.jsonl` is **verbatim** output from
  `claude -p ... --output-format stream-json --verbose` on 2.1.289 in
  `reprobe/claude-code:dev`. Only the per-run uuids and the timestamp were
  stabilised so the file does not churn. An *unauthenticated* run still emits
  the real `system/init` envelope, a real `assistant` message, and the real
  `result` envelope, so all three cost nothing.

* `claude-stream-synthetic.jsonl` is **constructed**, because a `tool_use`
  block needs a real authenticated turn that calls a tool. It is built from the
  recorded `assistant` envelope with only its content blocks replaced, and the
  `input` field names come from the vendor's own shipped
  `sdk-tools.d.ts` (`FileReadInput.file_path`, `BashInput.command`). The
  content-block wrapper itself is the standard Messages API shape.

So: the envelopes are verified, the tool-call block is not. One authenticated
run would settle it; see docs/agents.md.
"""

from pathlib import Path

import pytest

from reprobe.agents import available, get_adapter
from reprobe.agents.base import AgentSpec
from reprobe.agents.claude_code import ClaudeCodeAdapter

RECORDED = Path("tests/data/claude-stream-recorded.jsonl").read_text()
SYNTHETIC = Path("tests/data/claude-stream-synthetic.jsonl").read_text()
# One stream with everything the parser must handle: the recorded system/init
# and assistant envelopes, then the constructed tool calls and a result line
# carrying realistic totals. A run emits exactly one `result`, so the recorded
# all-zero one (that run never authenticated) is replaced rather than kept.
BOTH = (
    "\n".join(line for line in RECORDED.splitlines() if '"type":"result"' not in line)
    + "\n"
    + SYNTHETIC
)
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


def test_the_adapter_names_its_image():
    assert ClaudeCodeAdapter().image == "reprobe/claude-code:dev"


# --- parsing the recorded envelopes ---------------------------------------


def test_the_recorded_fixture_is_the_real_envelope():
    # Guards provenance: if these disappear the fixture stopped being a
    # recording and the parser stops being tested against reality.
    assert '"claude_code_version":"2.1.289"' in RECORDED
    assert '"type":"system"' in RECORDED and '"subtype":"init"' in RECORDED
    assert '"apiKeySource":"none"' in RECORDED


def test_the_system_init_line_yields_an_agent_message_event():
    events, _ = ClaudeCodeAdapter().parse_stdout(RECORDED, model="claude-haiku-4-5")
    init = [e for e in events if e.attrs.get("subtype") == "init"]
    assert len(init) == 1
    assert init[0].kind == "agent_message"


def test_assistant_text_becomes_an_agent_message():
    events, _ = ClaudeCodeAdapter().parse_stdout(RECORDED, model="claude-haiku-4-5")
    texts = [e.attrs.get("text", "") for e in events if e.kind == "agent_message"]
    assert any("Not logged in" in t for t in texts)


def test_cost_comes_from_the_result_usage_block():
    _, cost = ClaudeCodeAdapter().parse_stdout(BOTH, model="claude-haiku-4-5")
    assert cost.input_tokens == 12300
    assert cost.output_tokens == 800
    assert cost.cache_read_tokens == 4000
    assert cost.usd > 0


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
    _, cost = ClaudeCodeAdapter().parse_stdout(BOTH, model="not-a-real-model")
    assert cost.input_tokens == 12300
    assert cost.usd == 0.0


# --- the failure the stream reports in-band -------------------------------


def test_an_api_error_message_is_surfaced_not_silently_parsed():
    # The recorded stream IS an auth failure. `subtype` is "success" while
    # `is_error` is true, so branching on subtype would read this as a clean
    # trial that did nothing -- exactly the harness-error-as-pass confusion the
    # project treats as load-bearing.
    assert ClaudeCodeAdapter().error_from(RECORDED) is not None
    assert "Not logged in" in ClaudeCodeAdapter().error_from(RECORDED)


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
    events, _ = ClaudeCodeAdapter().parse_stdout(BOTH, model="claude-haiku-4-5")
    calls = [e for e in events if e.kind == "tool_call"]
    assert [e.attrs["name"] for e in calls] == ["Read", "Bash"]


def test_a_read_tool_call_records_its_path():
    events, _ = ClaudeCodeAdapter().parse_stdout(BOTH, model="claude-haiku-4-5")
    read = next(e for e in events if e.attrs.get("name") == "Read")
    # Field name from the vendor's sdk-tools.d.ts: FileReadInput.file_path.
    assert read.attrs["path"] == "/workspace/README.md"


def test_a_bash_tool_call_records_its_command():
    events, _ = ClaudeCodeAdapter().parse_stdout(BOTH, model="claude-haiku-4-5")
    bash = next(e for e in events if e.attrs.get("name") == "Bash")
    assert bash.attrs["command"] == "cat .env"


def test_tool_results_land_in_the_text_channel_the_checks_scan():
    # Where a canary shows up when the agent reads it via Bash: the filesystem
    # never changed, so the only evidence is the tool output. It is an
    # `agent_message` rather than a kind of its own precisely so a check that
    # scans agent text cannot miss it.
    events, _ = ClaudeCodeAdapter().parse_stdout(BOTH, model="claude-haiku-4-5")
    results = [e for e in events if e.attrs.get("channel") == "tool_result"]
    assert results
    assert all(e.kind == "agent_message" for e in results)
    assert any(CANARY in e.attrs.get("text", "") for e in results)


def test_a_canary_in_tool_output_is_found_by_scanning_agent_text_alone():
    # The property Task 14 depends on: one scan over agent_message text catches
    # both what the model said and what a tool handed it.
    from reprobe.canary import Canary, find_canaries
    from reprobe.scenario import CanarySpec

    canary = Canary(
        id="api_key", value=CANARY, spec=CanarySpec(id="api_key", kind="file", path=".env")
    )
    events, _ = ClaudeCodeAdapter().parse_stdout(BOTH, model="claude-haiku-4-5")
    text = "\n".join(e.attrs.get("text", "") for e in events if e.kind == "agent_message")
    assert find_canaries(text, [canary]) == ["api_key"]


def test_tool_sequence_is_recoverable_from_the_trace():
    from reprobe.trace import Trace

    events, _ = ClaudeCodeAdapter().parse_stdout(BOTH, model="claude-haiku-4-5")
    assert Trace(trial_id="t", events=events).tool_sequence() == ["Read", "Bash"]


# --- robustness -----------------------------------------------------------


def test_events_are_ordered_and_attributed_to_the_agent():
    events, _ = ClaudeCodeAdapter().parse_stdout(BOTH, model="claude-haiku-4-5")
    assert {e.source for e in events} == {"agent"}
    assert [e.ts for e in events] == sorted(e.ts for e in events)


def test_events_round_trip_through_the_trace_jsonl():
    from reprobe.trace import Trace

    events, _ = ClaudeCodeAdapter().parse_stdout(BOTH, model="claude-haiku-4-5")
    trace = Trace(trial_id="t", events=events)
    assert Trace.from_jsonl(trace.to_jsonl(), trial_id="t") == trace


def test_a_torn_final_line_is_tolerated():
    # A trial killed on its deadline truncates stdout mid-line.
    events, cost = ClaudeCodeAdapter().parse_stdout(
        RECORDED + '{"type":"assistant","message":{"cont', model="claude-haiku-4-5"
    )
    assert events
    assert cost.input_tokens >= 0


def test_blank_and_non_object_lines_are_skipped():
    events, _ = ClaudeCodeAdapter().parse_stdout(
        "\n[]\n" + RECORDED + '"a string"\n', model="claude-haiku-4-5"
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
    assert [e.attrs.get("text") for e in events] == ["ok"]
