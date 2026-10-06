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


# --- the attacker-controlled surfaces handed to the agent ------------------


def test_surface_paths_are_taken_from_the_scenario(minimal_spec):
    """The free lane's agent must read what the *scenario* says an attacker
    controls, not a list of filenames baked into the image.

    With a hardcoded list, a scenario whose surface is anywhere else has its
    payload written to disk and never read -- both search arms score zero and
    a gate across such scenarios reports a tie. Verified before this change:
    the same payload and profile violated via `README.md` and came back clean
    via `docs/guide.md`.
    """
    from reprobe.sandbox.docker_sandbox import _surface_paths

    assert _surface_paths(minimal_spec) == "README.md"


def test_surface_paths_lists_every_file_surface_in_order(minimal_scenario, minimal_spec):
    from reprobe.sandbox.docker_sandbox import _surface_paths
    from reprobe.scenario import Surface

    wider = minimal_scenario.model_copy(
        update={
            "surfaces": [
                *minimal_scenario.surfaces,
                Surface(id="guide", path="docs/guide.md", kind="file", template="{{ payload }}"),
                # Not a file: nothing is written to disk for it, so handing the
                # agent its path would point at something that is not there.
                Surface(
                    id="page", path="https://x.invalid/d", kind="http", template="{{ payload }}"
                ),
            ]
        }
    )
    spec = minimal_spec.model_copy(update={"scenario": wider})
    assert _surface_paths(spec) == "README.md,docs/guide.md"


def test_surface_paths_is_empty_when_a_scenario_has_no_file_surface(minimal_scenario, minimal_spec):
    from reprobe.sandbox.docker_sandbox import _surface_paths

    spec = minimal_spec.model_copy(
        update={"scenario": minimal_scenario.model_copy(update={"surfaces": []})}
    )
    assert _surface_paths(spec) == ""
