"""Shared fixtures and the opt-in gates for the expensive test lanes.

`pyproject.toml` documents `docker` as "skipped unless REPROBE_DOCKER_TESTS=1"
and `agent` as "skipped unless REPROBE_AGENT_TESTS=1". `addopts` deselects both
markers by default, but a developer who runs `pytest -m docker` has overridden
that and would otherwise reach for a daemon, or spend money, without having
opted in. These hooks make the documented contract true.

If the lane IS opted into and the daemon is missing, the tests fail rather than
skip: a silent skip is how a broken sandbox ships green.
"""

import os

import pytest

_LANES = {
    "docker": ("REPROBE_DOCKER_TESTS", "needs a Docker daemon"),
    "agent": ("REPROBE_AGENT_TESTS", "spends real agent/API budget"),
    # Free, but minutes rather than seconds: a statistical claim needs hundreds
    # of real trials. The per-PR `integration` job has a 25-minute budget that
    # also builds four images, so these run in the nightly soak instead.
    "slow": ("REPROBE_SLOW_TESTS", "takes minutes of real trials"),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for marker, (env_var, why) in _LANES.items():
        if os.environ.get(env_var) == "1":
            continue
        skip = pytest.mark.skip(reason=f"{why}; set {env_var}=1 to run")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


# --- shared scenario / trial fixtures -------------------------------------

import random  # noqa: E402
from pathlib import Path  # noqa: E402

from reprobe.agents.base import AgentSpec  # noqa: E402
from reprobe.canary import mint  # noqa: E402
from reprobe.sandbox import TrialSpec  # noqa: E402
from reprobe.scenario import load_scenario  # noqa: E402

MINIMAL_PATH = Path("tests/data/scenarios/minimal/scenario.yaml")


@pytest.fixture
def minimal_scenario():
    return load_scenario(MINIMAL_PATH)


@pytest.fixture
def minimal_spec(minimal_scenario):
    rng = random.Random(0)
    return TrialSpec(
        trial_id="trial_fixture",
        scenario=minimal_scenario,
        agent=AgentSpec(id="claude-code", model="claude-haiku-4-5"),
        payloads={"readme": "benign text"},
        canaries=[mint(c, rng) for c in minimal_scenario.canaries],
        seed=0,
    )


@pytest.fixture
def probes():
    """Register the probe adapters into the live registry for a test.

    DockerSandbox resolves an agent via `get_adapter(spec.agent.id)`, so the
    probes have to be in the real registry, not injected some other way.
    Removed again afterwards so one test cannot see another's.
    """
    from reprobe.agents import _ADAPTERS

    added = {pid: ProbeAdapter(pid) for pid in PROBE_COMMANDS}
    _ADAPTERS.update(added)
    try:
        yield
    finally:
        for pid in added:
            _ADAPTERS.pop(pid, None)


# --- probe adapters (used by the `probes` fixture above) ------------------

from reprobe.budget import Cost  # noqa: E402
from reprobe.trace import Event  # noqa: E402

# Each probe is (id -> shell command). The command runs under the image's
# strace entrypoint, so its syscalls are observed like any agent's.
PROBE_COMMANDS: dict[str, str] = {
    # Ignores the proxy and dials a real host directly. Uses a raw IP, not a
    # hostname, so it bypasses the DNS sinkhole and genuinely tests the
    # `internal` network guarantee (the sinkhole is tested separately in the
    # gateway suite). On an internal network there is no route, so curl prints
    # 000. 1.1.1.1 is a stable public resolver; the assertion is "unreachable",
    # which holds regardless of whether it happens to be up.
    "probe-curl": "curl --noproxy '*' -s -m5 -so /dev/null -w '%{http_code}' https://1.1.1.1||true;echo",
    # Dumps the environment, so a test can assert a host secret did not leak in.
    "probe-env": "env",
    # Exits immediately with output, for the reaping test.
    "probe-true": "echo probe-ok",
    # Sleeps past any sane trial deadline, for the timeout test.
    "probe-sleep": "sleep 600",
    # Writes a file into the workspace, for the fs-diff test.
    "probe-write": "echo probe > /workspace/PROBE.md; echo wrote",
    # Reports an in-band failure, for the harness-error mapping test. Emits the
    # Claude-style result envelope the adapter's error_from reads.
    "probe-fail": (
        "printf '%s\\n' "
        '\'{"type":"result","subtype":"success","is_error":true,'
        '"usage":{"input_tokens":0,"output_tokens":0},'
        '"result":"probe failure"}\''
    ),
}


class ProbeAdapter:
    image = "reprobe/base:dev"
    env_allowlist: tuple[str, ...] = ()
    infra_hosts: tuple[str, ...] = ()
    enforces_max_usd = False

    def __init__(self, probe_id: str) -> None:
        self.id = probe_id

    def command(self, spec: AgentSpec, *, task: str, max_usd: float) -> list[str]:
        return ["sh", "-lc", PROBE_COMMANDS[self.id]]

    def login_command(self) -> list[str] | None:
        return None

    def version_from(self, text: str) -> str:
        return "probe"

    def error_from(self, text: str) -> str | None:
        # probe-fail emits a real result envelope; reuse the Claude parser's
        # logic so the mapping under test is the real one.
        from reprobe.agents.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter().error_from(text)

    def parse_stdout(self, text: str, *, model: str = "") -> tuple[list[Event], Cost]:
        events = [Event(ts=0.0, kind="agent_message", source="agent", attrs={"text": text})]
        return events, Cost.zero()


# --- fake sandboxes for the CLI tests -------------------------------------


def _cli_result(*, leak=False, harness_error=None):
    from reprobe.agents.base import AgentMeta
    from reprobe.sandbox import FsDiff, TrialResult
    from reprobe.trace import Event, Trace

    events = [Event(ts=1.0, kind="tool_call", source="agent", attrs={"name": "Read"})]
    if leak:
        events.append(
            Event(
                ts=2.0,
                kind="file_read",
                source="strace",
                attrs={"path": "/workspace/.env", "ok": True},
            )
        )
    return TrialResult(
        trial_id="t",
        exit_code=0,
        trace=Trace(trial_id="t", events=events),
        fs_diff=FsDiff(),
        agent_meta=AgentMeta(
            agent_id="claude-code", agent_version="1", model_id="m", prompt_hash="p"
        ),
        harness_error=harness_error,
    )


@pytest.fixture
def fake_clean_sandbox():
    from reprobe.sandbox.fake import FakeSandbox

    return FakeSandbox(lambda spec: _cli_result())


@pytest.fixture
def fake_leaky_sandbox():
    from reprobe.sandbox.fake import FakeSandbox

    return FakeSandbox(lambda spec: _cli_result(leak=True))


@pytest.fixture
def fake_broken_sandbox():
    from reprobe.sandbox.fake import FakeSandbox

    return FakeSandbox(lambda spec: _cli_result(harness_error="container died"))


@pytest.fixture
def recording_sandbox():
    from reprobe.sandbox.fake import FakeSandbox

    return FakeSandbox(lambda spec: _cli_result())
