"""Observers turn raw sandbox artefacts into `Event`s. One file per source.

Observation is triangulated on purpose: the agent's own output says what it
claims it did, `strace` says what the process did, the filesystem diff says
what changed, and the gateway says what tried to leave. No single source is
trusted, because the PRD's false-positive target dies if an agent's
self-report is taken as ground truth.
"""

from __future__ import annotations

from typing import Protocol

from reprobe.trace import Event


class Observer(Protocol):
    source: str

    def events(self) -> list[Event]: ...
