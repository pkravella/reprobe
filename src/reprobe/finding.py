"""A confirmed, shrunk, pinned finding -- the unit of value in Reprobe.

Everything R11 needs to generate a test that means the same thing next month
lives on this model, including the sandbox description (image digests) and the
threshold the rate was judged against.

**The counts are the data; the interval is derived.** `successes` and `trials`
are what was observed, and `lo`/`hi`/`point` are a function of them. So
`from_record` recomputes the interval from the counts rather than trusting the
stored bounds. That is the safe direction twice over: a record written by an
older version with a different `z` is re-read consistently, and a hand-edited
findings file cannot talk a finding over the threshold.

`shrunk_bytes` is checked against the payloads it claims to describe.
`reduction` is the Phase-3 gate's headline number and it is computed from that
field, so a finding whose byte count disagrees with its own payload would report
a reduction for a payload nobody has -- the same class of bug as a shrinker
reporting a rate for a candidate it did not keep.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field, model_validator

from reprobe import __version__
from reprobe.agents.base import AgentMeta
from reprobe.ids import digest, new_id
from reprobe.mutate import Mutation
from reprobe.stats import DEFAULT_THRESHOLD, RateEstimate, estimate


class Finding(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    id: str = Field(default_factory=lambda: new_id("find"))
    scenario_name: str
    scenario_hash: str
    agent_meta: AgentMeta
    sandbox_description: dict[str, Any] = Field(default_factory=dict)
    payloads: dict[str, str]
    lineage: list[Mutation] = Field(default_factory=list)
    seed_ids: list[str] = Field(default_factory=list)
    action_keys: list[str]
    coverage_signature: str
    rate: RateEstimate
    threshold: float = DEFAULT_THRESHOLD
    original_bytes: int = 0
    #: Defaults to the actual size of `payloads`; a disagreeing value is refused.
    shrunk_bytes: int = 0
    env_removed: list[str] = Field(default_factory=list)
    trial_ids: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    created_at: float = Field(default_factory=time.time)
    reprobe_version: str = __version__

    @model_validator(mode="after")
    def _bytes_match_the_payload(self) -> Finding:
        actual = sum(len(v.encode("utf-8")) for v in self.payloads.values())
        if self.shrunk_bytes == 0:
            object.__setattr__(self, "shrunk_bytes", actual)
        elif self.shrunk_bytes != actual:
            raise ValueError(
                f"shrunk_bytes is {self.shrunk_bytes} but the payloads are {actual} bytes; "
                "the reported reduction would describe a payload this finding does not carry"
            )
        return self

    @property
    def reduction(self) -> float:
        if not self.original_bytes:
            return 0.0
        return 1.0 - self.shrunk_bytes / self.original_bytes

    def fingerprint(self) -> str:
        """Content identity: the same payload for the same bug in the same
        environment is the same finding, whatever run produced it."""
        return digest(
            {
                "payloads": self.payloads,
                "actions": sorted(self.action_keys),
                "scenario": self.scenario_hash,
            },
            prefix="find",
        )

    def title(self) -> str:
        """One line a human can scan in a list of twenty."""
        if not self.action_keys:
            return f"violation ({self.rate.summary()})"
        check, _, target = self.action_keys[0].partition(":")
        head = f"{check} → {target}" if target else check
        return f"{head} ({self.rate.summary()})"

    def to_record(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Finding:
        data = dict(record)
        rate = data.pop("rate")
        return cls(rate=estimate(int(rate["successes"]), int(rate["trials"])), **data)


__all__ = ["Finding"]
