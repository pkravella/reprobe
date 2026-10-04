"""R2: the agent seam.

Adding an agent (R13) is one adapter file plus one Dockerfile; nothing else
moves. The registry is explicit rather than discovered by import scanning, so
`available()` is the complete list and a typo in an id fails loudly.
"""

from __future__ import annotations

from reprobe.agents.base import AgentAdapter, AgentMeta, AgentSpec
from reprobe.agents.claude_code import ClaudeCodeAdapter
from reprobe.agents.codex_cli import CodexCliAdapter
from reprobe.agents.fake_agent import FakeAgentAdapter

_ADAPTERS: dict[str, AgentAdapter] = {
    ClaudeCodeAdapter.id: ClaudeCodeAdapter(),
    CodexCliAdapter.id: CodexCliAdapter(),
    FakeAgentAdapter.id: FakeAgentAdapter(),
}


def available() -> list[str]:
    return sorted(_ADAPTERS)


def get_adapter(agent_id: str) -> AgentAdapter:
    try:
        return _ADAPTERS[agent_id]
    except KeyError:
        raise KeyError(f"unknown agent {agent_id!r}; known agents: {available()}") from None


__all__ = ["AgentAdapter", "AgentMeta", "AgentSpec", "available", "get_adapter"]
