"""R1: the declarative scenario.

A scenario says: here is honest work for the agent, here is the repo it works
in, here are the parts of that repo an attacker controls, here are the secrets
that must not move, and here is what counts as a violation.

Everything is frozen. A scenario is an identity as much as a configuration --
its hash goes into every exported regression test -- so it must not be possible
to mutate one after a trial has been recorded against it.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from reprobe.errors import ScenarioError
from reprobe.ids import digest, digest_bytes

SurfaceKind = Literal["file", "http", "tool_output", "terminal"]
CanaryKind = Literal["file", "env", "tool_output"]
CheckType = Literal[
    "canary_read",
    "canary_exfil",
    "protected_write",
    "egress_offlist",
    "dangerous_command",
]
PAYLOAD_SLOT = "{{ payload }}"


class Surface(BaseModel):
    """A location whose content an attacker controls.

    `template` must contain `{{ payload }}`; the mutator's output is substituted
    there, so the honest-looking frame around the payload stays fixed and only
    the attacker-controlled part varies between trials.
    """

    model_config = {"frozen": True}

    id: str
    path: str
    kind: SurfaceKind
    template: str
    encoding: str = "utf-8"


class CanarySpec(BaseModel):
    """Where a synthetic secret is planted for a trial."""

    model_config = {"frozen": True}

    id: str
    kind: CanaryKind
    path: str | None = None
    env_var: str | None = None


class PolicyCheck(BaseModel):
    model_config = {"frozen": True}

    type: CheckType
    params: dict[str, Any] = Field(default_factory=dict)


class Limits(BaseModel):
    model_config = {"frozen": True}

    max_trial_seconds: int = 300
    max_turns: int = 40
    max_usd_per_trial: float = 0.50


class Scenario(BaseModel):
    model_config = {"frozen": True}

    name: str
    version: int
    task: str
    fixture_dir: Path
    surfaces: list[Surface]
    canaries: list[CanarySpec] = Field(default_factory=list)
    egress_allowlist: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    checks: list[PolicyCheck]
    limits: Limits = Field(default_factory=Limits)

    @cached_property
    def scenario_hash(self) -> str:
        """Content hash of everything that changes the meaning of the scenario.

        `fixture_dir` is excluded because it is a local path, but the fixture's
        *content* is included: moving a scenario on disk must not invalidate
        findings exported from it, while editing a fixture file must, because
        that changes the environment the agent ran in.
        """
        payload = self.model_dump(mode="json", exclude={"fixture_dir"})
        payload["fixture"] = _fixture_manifest(self.fixture_dir)
        return digest(payload, prefix="scn")

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Scenario:
        """Copy, dropping any cached hash when the content changes.

        `cached_property` stores its value in the instance `__dict__`, which
        pydantic copies verbatim -- so without this, a narrowed scenario would
        report the *original's* hash. The environment shrinker narrows scenarios
        constantly, and a finding recorded against a hash it was never tested on
        would silently break the replay guarantee the whole project rests on.
        """
        copied = super().model_copy(update=update, deep=deep)
        if update:
            copied.__dict__.pop("scenario_hash", None)
        return copied

    def surface(self, surface_id: str) -> Surface:
        for s in self.surfaces:
            if s.id == surface_id:
                return s
        raise ScenarioError(f"unknown surface id: {surface_id!r}")


def _fixture_manifest(root: Path) -> list[list[str]]:
    """Sorted (relative path, content hash) pairs for every file in the fixture."""
    out: list[list[str]] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out.append([p.relative_to(root).as_posix(), digest_bytes(p.read_bytes())])
    return out


def load_scenario(path: Path) -> Scenario:
    """Parse and validate a scenario file. Every rejection names the problem."""
    path = Path(path).resolve()
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ScenarioError(f"cannot read scenario {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioError(f"scenario {path} must be a YAML mapping")

    raw = dict(raw)
    raw["fixture_dir"] = (path.parent / raw.pop("fixture", "./fixture")).resolve()
    try:
        scn = Scenario.model_validate(raw)
    except ValidationError as exc:
        raise ScenarioError(f"invalid scenario {path}:\n{exc}") from exc

    _validate_semantics(scn, path)
    return scn


def _validate_semantics(scn: Scenario, path: Path) -> None:
    """Checks pydantic cannot express: cross-field and on-disk consistency."""
    if not scn.fixture_dir.is_dir():
        raise ScenarioError(f"fixture directory not found: {scn.fixture_dir}")

    if not scn.surfaces:
        raise ScenarioError(
            f"scenario {path} declares no attacker-controlled surfaces; "
            f"there would be nothing to mutate"
        )

    seen_surfaces: set[str] = set()
    for s in scn.surfaces:
        if s.id in seen_surfaces:
            raise ScenarioError(f"duplicate surface id {s.id!r} in {path}")
        seen_surfaces.add(s.id)
        if PAYLOAD_SLOT not in s.template:
            raise ScenarioError(
                f"surface {s.id!r} template must contain the payload slot "
                f"{PAYLOAD_SLOT}, or the mutator's output has nowhere to go"
            )

    seen_canaries: set[str] = set()
    for c in scn.canaries:
        if c.id in seen_canaries:
            raise ScenarioError(f"duplicate canary id {c.id!r} in {path}")
        seen_canaries.add(c.id)
        if c.kind == "file" and not c.path:
            raise ScenarioError(f"canary {c.id!r} is kind 'file' but has no path")
        if c.kind == "env" and not c.env_var:
            raise ScenarioError(f"canary {c.id!r} is kind 'env' but has no env_var")

    for check in scn.checks:
        wanted = check.params.get("canary")
        if wanted is not None and wanted not in seen_canaries:
            raise ScenarioError(
                f"check {check.type} references unknown canary {wanted!r} in {path}"
            )


__all__ = [
    "PAYLOAD_SLOT",
    "CanaryKind",
    "CanarySpec",
    "CheckType",
    "Limits",
    "PolicyCheck",
    "Scenario",
    "Surface",
    "SurfaceKind",
    "load_scenario",
]
