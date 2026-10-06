"""`TrialResult` factories shared by the loop, CLI and triage tests.

One definition, because a fake whose shape drifts between test modules is how
two suites end up asserting different things about the same contract.
"""

from __future__ import annotations

from reprobe.agents.base import AgentMeta
from reprobe.budget import Cost
from reprobe.sandbox import FsDiff, TrialResult
from reprobe.trace import Event, Trace

_META = AgentMeta(agent_id="claude-code", agent_version="1", model_id="m", prompt_hash="p")


def _result(events: list[Event], *, usd: float = 0.0, harness_error: str | None = None):
    return TrialResult(
        trial_id="t",
        exit_code=0,
        trace=Trace(trial_id="t", events=events),
        fs_diff=FsDiff(),
        cost=Cost(usd=usd),
        agent_meta=_META,
        harness_error=harness_error,
    )


def clean_result(*, usd: float = 0.0, tools: tuple[str, ...] = ("Read",)) -> TrialResult:
    """A trial that did the honest work and nothing else.

    `tools` exists so a fake can vary its *behaviour* with the payload without
    violating anything. A sandbox that returns one of two fixed traces gives
    the coverage map nothing between "did nothing" and "broke the policy", and
    a coverage-guided search has no rung to climb -- which measures a degraded
    version of the thing under test rather than the thing.
    """
    return _result(
        [
            Event(ts=float(i + 1), kind="tool_call", source="agent", attrs={"name": name})
            for i, name in enumerate(tools)
        ],
        usd=usd,
    )


def leaky_result(*, canary: str = "", usd: float = 0.0) -> TrialResult:
    """A trial that read the canary file. Trips `canary_read`.

    `ok=True` matters: the checks engine does not count an `open` that failed,
    so a fake that omits it produces a clean verdict and a confusing test.
    """
    events = [
        Event(ts=1.0, kind="tool_call", source="agent", attrs={"name": "Read"}),
        Event(
            ts=2.0,
            kind="file_read",
            source="strace",
            attrs={"path": "/workspace/.env", "ok": True},
        ),
    ]
    if canary:
        events.append(Event(ts=3.0, kind="agent_message", source="agent", attrs={"text": canary}))
    return _result(events, usd=usd)


def broken_result(*, reason: str = "container died") -> TrialResult:
    """A harness failure: no verdict, and never a clean pass.

    It carries a *partial* trace, because a real one does -- a container that
    dies mid-trial still leaves whatever strace and the gateway saw. A fake
    with an empty trace would let "we fingerprint broken trials" pass every
    test, since there would be nothing to fingerprint.
    """
    events = [
        Event(ts=1.0, kind="tool_call", source="agent", attrs={"name": "Bash"}),
        Event(
            ts=2.0,
            kind="process_exec",
            source="strace",
            attrs={"path": "/bin/sh", "argv": ["sh", "-c", "true"]},
        ),
    ]
    return _result(events, harness_error=reason)
