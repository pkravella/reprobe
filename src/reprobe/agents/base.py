"""The agent data contracts.

An adapter is the only place a vendor's CLI flags and output format appear. Add
an agent (R13) by adding one file here plus one Dockerfile; nothing else moves.

Adapters must never modify the agent. "Test agents as shipped" is PRD goal 4:
we drive the published CLI with published flags and parse its published output.

The `AgentAdapter` protocol itself lands in Task 11, against recorded output
from a real CLI. Only the two models the sandbox needs are declared here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
