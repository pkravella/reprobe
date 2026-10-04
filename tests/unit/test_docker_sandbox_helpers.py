"""Pure helpers from the Docker sandbox -- no daemon, so they run on every PR."""

from reprobe.sandbox.docker_sandbox import compose_command


def test_compose_command_passes_the_agent_through_untouched():
    assert compose_command(None, ["codex", "exec", "x"]) == ["codex", "exec", "x"]


def test_compose_command_runs_login_first_then_execs_the_agent():
    composed = compose_command(
        ["sh", "-lc", "printenv OPENAI_API_KEY | codex login --with-api-key"],
        ["codex", "exec", "the task"],
    )
    assert composed[0] == "sh"
    joined = composed[-1]
    assert "codex login" in joined
    assert joined.index("login") < joined.index("exec"), "login must run before the agent"
    assert "'the task'" in joined, "the agent task must stay one shell-quoted argument"
