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

    # `max_usd` rather than `max_turns`: Claude Code has no turn cap, and a
    # per-trial dollar cap is the one limit every supported agent can express.
    def command(self, spec: AgentSpec, *, task: str, max_usd: float) -> list[str]: ...

    def parse_stdout(self, text: str, *, model: str = "") -> tuple[list[Event], Cost]: ...

    def version_from(self, text: str) -> str: ...

    def error_from(self, text: str) -> str | None:
        """A failure the agent reported in its own output stream.

        Separate from `parse_stdout` because it decides whether the trial has a
        verdict at all: an auth failure or a budget abort is a `HarnessError`,
        not a clean run with no violations.
        """
        ...
