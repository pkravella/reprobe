"""A sandbox that never starts a container.

This is how the search loop, the rate estimator, the shrinker, the dedupe pass
and the exporters are tested: give `FakeSandbox` a function from `TrialSpec` to
`TrialResult` and you can model any agent behaviour, including a flaky one, for
free and in milliseconds. It is the workhorse of every test from Task 16 on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from reprobe.ids import digest
from reprobe.sandbox import TrialResult, TrialSpec

Behaviour = Callable[[TrialSpec], TrialResult]


class FakeSandbox:
    def __init__(self, results: Behaviour | dict[str, TrialResult]) -> None:
        self._results = results
        self.calls: list[TrialSpec] = []

    def run(self, spec: TrialSpec) -> TrialResult:
        self.calls.append(spec)
        if callable(self._results):
            result = self._results(spec)
        else:
            key = digest(spec.payloads)
            if key in self._results:
                result = self._results[key]
            elif "*" in self._results:
                result = self._results["*"]
            else:
                raise KeyError(f"FakeSandbox has no result for payloads {spec.payloads!r}")
        # The spec's trial id is authoritative so a caller can correlate; a
        # canned result built with its own id must not override it, and the
        # trace has to be rewritten too or a stored trace points at the wrong
        # trial.
        return result.model_copy(
            update={
                "trial_id": spec.trial_id,
                "trace": result.trace.model_copy(update={"trial_id": spec.trial_id}),
            }
        )

    def describe(self) -> dict[str, Any]:
        return {"runtime": "fake"}
