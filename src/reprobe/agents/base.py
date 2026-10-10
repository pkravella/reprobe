"""The agent data contracts.

An adapter is the only place a vendor's CLI flags and output format appear. Add
an agent (R13) by adding one file here plus one Dockerfile; nothing else moves.

Adapters must never modify the agent. "Test agents as shipped" is PRD goal 4:
we drive the published CLI with published flags and parse its published output.

"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from reprobe.budget import Cost
from reprobe.trace import Event


class AgentSpec(BaseModel):
    """Which agent to run, and how. The request side."""

    model_config = {"frozen": True}

    id: str
    model: str
    version_pin: str | None = None
    extra_args: list[str] = Field(default_factory=list)


class AgentMeta(BaseModel):
    """What actually ran. Everything R11 pins an exported test to.

    Distinct from `AgentSpec` on purpose: a spec may say `model="sonnet"` and an
    alias may resolve to something else, so a finding records the resolved
    identity rather than the request that produced it.
    """

    model_config = {"frozen": True}

    agent_id: str
    agent_version: str
    model_id: str
    prompt_hash: str
    container_digest: str = ""


class AgentAdapter(Protocol):
    """The only place a vendor's CLI flags and output format appear."""

    id: str
    image: str
    env_allowlist: tuple[str, ...]
    #: The subset of `env_allowlist` the agent cannot run without. An exported
    #: suite fails, rather than skips, when one is missing after opting in.
    required_env: tuple[str, ...]
    #: Model-API hosts this agent must reach. The sandbox dual-homes the gateway
    #: and lets a CONNECT tunnel to exactly these; checks never flag them.
    infra_hosts: tuple[str, ...]

    # `max_usd` rather than `max_turns`: Claude Code has no turn cap, and a
    # per-trial dollar cap is the one limit every supported agent can express.
    def command(self, spec: AgentSpec, *, task: str, max_usd: float) -> list[str]: ...

    def parse_stdout(self, text: str, *, model: str = "") -> tuple[list[Event], Cost]: ...

    def version_from(self, text: str) -> str: ...

    #: Whether the CLI itself honours a per-trial dollar cap. False means the
    #: budget ledger is the only thing enforcing one -- declared rather than
    #: silently assumed, since Codex has no such flag and Claude Code does.
    enforces_max_usd: bool

    def login_command(self) -> list[str] | None:
        """A command to run before the agent, or None.

        Codex will not accept OPENAI_API_KEY from the environment: the key has
        to be piped into `codex login --with-api-key` on stdin first. Claude
        Code needs nothing. The difference lives here rather than in the
        sandbox so adding an agent stays one file.
        """
        ...

    def error_from(self, text: str) -> str | None:
        """A failure the agent reported in its own output stream.

        Separate from `parse_stdout` because it decides whether the trial has a
        verdict at all: an auth failure or a budget abort is a `HarnessError`,
        not a clean run with no violations.
        """
        ...
