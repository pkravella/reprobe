"""Exception hierarchy. Everything Reprobe raises on purpose derives from ReprobeError."""

from __future__ import annotations


class ReprobeError(Exception):
    """Base class for all intentional Reprobe failures."""


class ScenarioError(ReprobeError):
    """A scenario file is invalid or its fixture is missing."""


class HarnessError(ReprobeError):
    """The harness itself failed: the sandbox, an observer, or an agent adapter.

    A HarnessError means the trial produced no usable verdict. It is never a violation.
    Phase-1's exit gate counts these: 100 trials, zero HarnessErrors.
    """


class BudgetExceeded(ReprobeError):
    """A dollar, trial, or wall-clock cap tripped. The loop stops."""


__all__ = ["BudgetExceeded", "HarnessError", "ReprobeError", "ScenarioError"]
