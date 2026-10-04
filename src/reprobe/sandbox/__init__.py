"""The sandbox seam.

Search and triage depend only on `SandboxProtocol`. That is what lets every
test above Task 15 run with `FakeSandbox` and zero dollars, and what makes a
remote runner (P2, R18) a new implementation rather than a rewrite.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from reprobe.agents.base import AgentMeta, AgentSpec
from reprobe.budget import Cost
from reprobe.canary import Canary
from reprobe.scenario import Scenario
from reprobe.trace import Trace


class FsDiff(BaseModel):
    """What changed on disk over a trial.

    Content only. A mode change (`chmod`) does not alter a file's bytes and so
    is invisible here; `strace` is the source for `permission_change`. This is
    the triangulation the PRD asks for working as intended, not a gap to paper
    over -- but do not read an empty `FsDiff` as "nothing happened".
    """

    model_config = {"frozen": True}

    created: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)

    @property
    def touched(self) -> list[str]:
        return sorted({*self.created, *self.modified, *self.deleted})


class EgressRecord(BaseModel):
    """One connection the mock gateway saw (Task 9).

    `headers` is carried because a canary can be smuggled in any header, not
    just a body or a query string. Searching only the body would leave that
    exfiltration path undetected.
    """

    model_config = {"frozen": True}

    ts: float
    host: str
    port: int
    method: str | None = None
    path: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body_preview: str = ""
    allowed: bool = False
    protocol: str = "http"


class TrialSpec(BaseModel):
    """Everything needed to run one trial, and nothing that varies within it."""

    model_config = {"frozen": True}

    trial_id: str
    scenario: Scenario
    agent: AgentSpec
    payloads: dict[str, str] = Field(default_factory=dict)
    canaries: list[Canary] = Field(default_factory=list)
    seed: int = 0
    env_overrides: dict[str, str] = Field(default_factory=dict)


class TrialResult(BaseModel):
    """The observed outcome of one trial, from all four observers."""

    model_config = {"frozen": True}

    trial_id: str
    exit_code: int
    trace: Trace
    fs_diff: FsDiff
    egress: list[EgressRecord] = Field(default_factory=list)
    cost: Cost = Field(default_factory=Cost.zero)
    agent_meta: AgentMeta
    duration_s: float = 0.0
    stdout_ref: str | None = None
    harness_error: str | None = None

    @property
    def ok(self) -> bool:
        """A usable trial. A harness error means no verdict, not a clean pass."""
        return self.harness_error is None


class SandboxProtocol(Protocol):
    def run(self, spec: TrialSpec) -> TrialResult: ...

    def describe(self) -> dict[str, Any]:
        """Pinnable identity: image digests, runtime, versions. Goes into R11 exports."""
        ...
