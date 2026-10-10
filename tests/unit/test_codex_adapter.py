"""Codex CLI adapter (R2).

Both fixtures are **verbatim recordings** from codex 0.160.0 in
`reprobe/codex-cli:dev`, with only per-run identifiers stabilised:

* `codex-stream-recorded.jsonl` — a real authenticated trial that ran a shell
  command successfully (`exit_code: 0`) and reported usage.
* `codex-stream-failed.jsonl` — a real unauthenticated run, trimmed from twelve
  identical retry lines to the distinct shapes. This is the error path.

Nothing here is constructed. The event schema is Codex's own and shares
nothing with Claude Code's beyond being JSONL.
"""

from pathlib import Path

import pytest

from reprobe.agents import get_adapter
from reprobe.agents.base import AgentSpec
from reprobe.agents.codex_cli import CodexCliAdapter

STREAM = Path("tests/data/codex-stream-recorded.jsonl").read_text()
FAILED = Path("tests/data/codex-stream-failed.jsonl").read_text()


def _spec(**kw) -> AgentSpec:
    return AgentSpec(**{"id": "codex-cli", "model": "gpt-5-codex", **kw})


def _events(stream: str = STREAM):
    return CodexCliAdapter().parse_stdout(stream, model="gpt-5-codex")[0]


# --- registry and identity ------------------------------------------------


def test_the_registry_exposes_the_codex_adapter():
    assert isinstance(get_adapter("codex-cli"), CodexCliAdapter)


def test_identity_and_image():
    adapter = CodexCliAdapter()
    assert adapter.id == "codex-cli"
    assert adapter.image == "reprobe/codex-cli:dev"


def test_env_allowlist_is_minimal():
    assert CodexCliAdapter().env_allowlist == ("OPENAI_API_KEY", "OPENAI_BASE_URL")


def test_required_env_is_the_api_key_and_nothing_optional():
    adapter = CodexCliAdapter()
    assert adapter.required_env == ("OPENAI_API_KEY",)
    assert set(adapter.required_env) <= set(adapter.env_allowlist)


# --- the command line -----------------------------------------------------


def test_command_is_the_non_interactive_subcommand_with_json_output():
    cmd = CodexCliAdapter().command(_spec(), task="do the thing", max_usd=0.25)
    assert cmd[0] == "codex"
    assert "exec" in cmd
    assert "--json" in cmd
    assert "do the thing" in cmd


def test_command_passes_the_model():
    cmd = CodexCliAdapter().command(_spec(), task="t", max_usd=0.5)
    assert cmd[cmd.index("--model") + 1] == "gpt-5-codex"


def test_command_bypasses_codexs_own_sandbox():
    # Measured: with `--sandbox read-only` every shell command fails inside a
    # container with "bwrap: No permissions to create a new namespace", because
    # Codex sandboxes with bubblewrap and that needs user namespaces. The agent
    # then cannot do anything, so no violation could ever be observed -- the
    # trial is silently useless rather than loudly broken.
    #
    # --dangerously-bypass-approvals-and-sandbox is documented as "intended
    # solely for running in environments that are externally sandboxed", which
    # is exactly what the trial container is. Verified: exit_code 0.
    cmd = CodexCliAdapter().command(_spec(), task="t", max_usd=0.5)
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd


def test_command_does_not_depend_on_a_git_repo():
    assert "--skip-git-repo-check" in CodexCliAdapter().command(_spec(), task="t", max_usd=1.0)


def test_command_keeps_the_trial_deterministic():
    # No host config bleeding in, no session files persisted between trials.
    cmd = CodexCliAdapter().command(_spec(), task="t", max_usd=1.0)
    assert "--ignore-user-config" in cmd
    assert "--ephemeral" in cmd


def test_extra_args_are_appended():
    cmd = CodexCliAdapter().command(_spec(extra_args=["-c", 'model="o3"']), task="t", max_usd=1.0)
    assert cmd[-2:] == ["-c", 'model="o3"']


def test_codex_has_no_budget_flag_so_max_usd_is_not_silently_dropped():
    # Claude Code has --max-budget-usd; codex exec has no equivalent. The cap
    # is enforced by the budget ledger instead, and the adapter says so rather
    # than inventing a flag.
    assert CodexCliAdapter().enforces_max_usd is False


# --- auth -----------------------------------------------------------------


def test_the_adapter_declares_that_it_needs_a_login_step():
    # OPENAI_API_KEY in the environment is NOT enough. Measured: with the key
    # present in the container, `codex exec` still returns
    # "401 Unauthorized: Missing bearer or basic authentication in header".
    # The key has to be fed through `codex login --with-api-key` on stdin
    # first. Claude Code needs no such step, so this cannot live in the shared
    # harness -- the adapter has to describe it.
    login = CodexCliAdapter().login_command()
    assert login is not None
    joined = " ".join(login)
    assert "codex login" in joined
    assert "--with-api-key" in joined


def test_the_login_command_never_puts_the_key_on_a_command_line():
    # It has to arrive on stdin. A key in argv is visible in the process list
    # and in any strace log of the trial.
    login = CodexCliAdapter().login_command()
    assert "OPENAI_API_KEY" not in " ".join(login[1:]) or "printenv" in " ".join(login)
    assert not any(part.startswith("sk-") for part in login)


def test_claude_code_needs_no_login_step():
    assert get_adapter("claude-code").login_command() is None


# --- parsing the real success stream --------------------------------------


def test_the_fixture_is_a_real_recording():
    assert '"type":"turn.completed"' in STREAM
    assert '"type":"command_execution"' in STREAM
    assert '"exit_code":0' in STREAM


def test_agent_messages_become_events_with_their_text():
    texts = [e.attrs.get("text", "") for e in _events() if e.kind == "agent_message"]
    assert any("README.md" in t for t in texts)


def test_a_command_execution_becomes_a_tool_call_in_order():
    calls = [e for e in _events() if e.kind == "tool_call"]
    assert [e.attrs["name"] for e in calls] == ["command_execution"]


def test_the_shell_command_is_preserved_verbatim():
    call = next(e for e in _events() if e.kind == "tool_call")
    assert call.attrs["command"] == "/bin/bash -lc 'cat README.md'"


def test_a_completed_command_reports_its_exit_code_and_output():
    call = next(e for e in _events() if e.kind == "tool_call")
    assert call.attrs["exit_code"] == 0
    assert "A widget library." in call.attrs["output"]


def test_an_item_is_emitted_once_even_though_it_is_reported_twice():
    # command_execution arrives as item.started then item.completed. Emitting
    # both would double every command in the trace and in the tool sequence.
    assert '"type":"item.started"' in STREAM
    assert len([e for e in _events() if e.kind == "tool_call"]) == 1


def test_command_output_is_scanned_for_canaries_by_landing_in_the_text_channel():
    # Same reason as the Claude adapter: a canary read by a shell command shows
    # up only in that command's output, so it has to reach the text a check
    # scans.
    from reprobe.canary import Canary, find_canaries
    from reprobe.scenario import CanarySpec

    value = "RPRB_CANARY_" + "B" * 32
    stream = (
        '{"type":"item.completed","item":{"id":"i1","type":"command_execution",'
        '"command":"cat .env","aggregated_output":"API_KEY=' + value + '",'
        '"exit_code":0,"status":"completed"}}\n'
    )
    canary = Canary(
        id="api_key", value=value, spec=CanarySpec(id="api_key", kind="file", path=".env")
    )
    events = _events(stream)
    text = "\n".join(str(e.attrs.get("text", "")) + str(e.attrs.get("output", "")) for e in events)
    assert find_canaries(text, [canary]) == ["api_key"]


# --- usage and cost -------------------------------------------------------


def test_usage_comes_from_turn_completed():
    _, cost = CodexCliAdapter().parse_stdout(STREAM, model="gpt-5-codex")
    assert cost.total_tokens > 0


def test_cached_tokens_are_a_subset_of_input_tokens_not_additive():
    # Measured across two real runs: input_tokens == cached_input_tokens +
    # cache_write_input_tokens + a small remainder (6 tokens in both). So
    # input_tokens is the TOTAL and the cache figures break it down. Passing
    # all three straight to price() bills ~25k tokens twice per trial, which
    # trips a budget cap early and makes every cost estimate wrong.
    stream = (
        '{"type":"turn.completed","usage":{"input_tokens":1000,'
        '"cached_input_tokens":600,"cache_write_input_tokens":300,'
        '"output_tokens":50,"reasoning_output_tokens":0}}\n'
    )
    _, cost = CodexCliAdapter().parse_stdout(stream, model="gpt-5-codex")
    assert cost.input_tokens == 100, "uncached input must exclude the cache figures"
    assert cost.cache_read_tokens == 600
    assert cost.cache_write_tokens == 300
    assert cost.output_tokens == 50
    # And the breakdown still adds back up to what the CLI reported.
    assert cost.input_tokens + cost.cache_read_tokens + cost.cache_write_tokens == 1000


def test_a_usage_block_without_cache_figures_still_works():
    stream = '{"type":"turn.completed","usage":{"input_tokens":500,"output_tokens":20}}\n'
    _, cost = CodexCliAdapter().parse_stdout(stream, model="gpt-5-codex")
    assert cost.input_tokens == 500
    assert cost.cache_read_tokens == 0


def test_an_unpriced_model_keeps_its_token_counts():
    _, cost = CodexCliAdapter().parse_stdout(STREAM, model="not-a-real-model")
    assert cost.total_tokens > 0
    assert cost.usd == 0.0


# --- the error path -------------------------------------------------------


def test_a_failed_turn_is_reported_as_an_error():
    error = CodexCliAdapter().error_from(FAILED)
    assert error is not None
    assert "401" in error


def test_a_successful_turn_reports_no_error():
    assert CodexCliAdapter().error_from(STREAM) is None


def test_a_stream_with_no_terminal_event_is_an_error():
    # A trial killed on its deadline never emits turn.completed. Treating that
    # as success would turn a timeout into a clean trial with no violations.
    assert CodexCliAdapter().error_from('{"type":"turn.started"}\n') is not None


def test_an_empty_stream_is_an_error_not_a_clean_pass():
    assert CodexCliAdapter().error_from("") is not None


# --- robustness -----------------------------------------------------------


def test_parse_stdout_on_garbage_yields_nothing():
    events, cost = CodexCliAdapter().parse_stdout("garbage\n", model="gpt-5-codex")
    assert events == [] and cost.usd == 0.0


def test_log_noise_on_the_same_stream_is_skipped():
    # Codex writes its ERROR log lines to stderr, but if the harness ever
    # merges the two the parser must not choke.
    noisy = (
        "2026-10-04T05:59:01.521814Z ERROR codex_api: failed to connect\n"
        + STREAM
        + "Reading additional input from stdin...\n"
    )
    assert _events(noisy)


def test_a_torn_final_line_is_tolerated():
    assert _events(STREAM + '{"type":"item.comple')


def test_events_are_ordered_and_attributed_to_the_agent():
    events = _events()
    assert {e.source for e in events} == {"agent"}
    assert [e.ts for e in events] == sorted(e.ts for e in events)


def test_events_round_trip_through_the_trace_jsonl():
    from reprobe.trace import Trace

    trace = Trace(trial_id="t", events=_events())
    assert Trace.from_jsonl(trace.to_jsonl(), trial_id="t") == trace


# --- version --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("codex-cli 0.160.0", "0.160.0"),
        ("codex-cli 0.9.3", "0.9.3"),
        ("0.160.0", "0.160.0"),
        ("", "unknown"),
    ],
)
def test_version_from_extracts_the_version(raw, expect):
    # Note the shape differs from Claude Code's "2.1.289 (Claude Code)": here
    # the version is the SECOND token, so a shared implementation would be
    # wrong for one of them.
    assert CodexCliAdapter().version_from(raw) == expect


def test_the_failure_stream_still_yields_its_error_messages_as_events():
    # The retry/error chatter is evidence too: a trial that failed on auth
    # should show why in its trace, not just be empty.
    events = _events(FAILED)
    errors = [e for e in events if e.attrs.get("channel") == "error"]
    assert errors
    assert all(e.kind == "agent_message" for e in errors)
    assert any("401" in e.attrs["text"] for e in errors)


def test_an_item_type_not_yet_seen_in_a_recording_is_skipped_not_guessed():
    # file_change, mcp_tool_call, web_search, reasoning, patch_apply and
    # todo_list exist in the binary's item-type set but have never appeared in
    # a recording here, so they are deliberately unmapped rather than mapped
    # on a guess. The raw stream is stored next to the trace, so a later
    # recording can map them without losing anything.
    stream = (
        '{"type":"item.completed","item":{"id":"i1","type":"web_search",'
        '"query":"something"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
    )
    assert _events(stream) == []
