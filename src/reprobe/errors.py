"""Exception hierarchy. Everything Reprobe raises on purpose derives from ReprobeError."""

from __future__ import annotations


class ReprobeError(Exception):
    """Base class for all intentional Reprobe failures."""


class ScenarioError(ReprobeError):
    """A scenario file is invalid or its fixture is missing."""


class SeedError(ReprobeError):
    """A seed file is invalid, or two seeds claim the same id.

    Seed id is provenance: it rides a candidate's lineage into every finding,
    so a silent collision would misattribute an exploit to the wrong seed.
    """


class ConfigError(ReprobeError):
    """A component was handed settings it cannot work with.

    Deliberately general: a coverage map sized so its own indices fall off the
    end, a signal group that does not exist, a scheduler weight that cannot be
    normalised. These share a failure mode -- the run continues and quietly
    measures nothing -- so they share an exception rather than one per module.
    """


class HarnessError(ReprobeError):
    """The harness itself failed: the sandbox, an observer, or an agent adapter.

    A HarnessError means the trial produced no usable verdict. It is never a violation.
    Phase-1's exit gate counts these: 100 trials, zero HarnessErrors.
    """


class BudgetExceeded(ReprobeError):
    """A dollar, trial, or wall-clock cap tripped. The loop stops."""


__all__ = [
    "BudgetExceeded",
    "ConfigError",
    "HarnessError",
    "ReprobeError",
    "ScenarioError",
    "SeedError",
]
