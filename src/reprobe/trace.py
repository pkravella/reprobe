"""R5: the trial trace.

The trace is the single input to both the checks (Task 14) and the coverage map
(Task 19), so its shape is load-bearing. It is stored as JSONL (one event per
line, cheap to append and to stream) and *also* exportable as OpenTelemetry
spans, because teams already have a trace viewer and a run timeline is the
fastest way for a human to understand a finding (R17).

Observation is triangulated: `source` records which observer produced an event
(`"agent"`, `"strace"`, `"fsdiff"`, `"gateway"`), so a check can prefer the
authoritative source for its signal rather than trusting the agent's own
account of what it did.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, field_validator

EventKind = Literal[
    "agent_message",
    "tool_call",
    "file_read",
    "file_write",
    "process_exec",
    "net_attempt",
    "permission_change",
    "memory_write",
]

#: Attribute value types OpenTelemetry accepts directly.
_OTEL_SCALARS = (str, bool, int, float)


def _json_native(value: Any) -> Any:
    """Coerce `value` into something `json.dumps` reproduces exactly.

    Events arrive from four observers that each build attributes their own way,
    and a tuple or a `Path` survives `model_dump` unchanged only to come back
    from JSON as a list or a string. Normalising on construction is what makes
    `from_jsonl(to_jsonl(t)) == t` true rather than true-for-the-values-we-
    happened-to-test-with.
    """
    if value is None or isinstance(value, _OTEL_SCALARS):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_native(item) for item in value]
    return str(value)


class Event(BaseModel):
    """One observed thing, at one instant, attributed to one observer.

    `frozen` blocks attribute assignment but does not deep-freeze `attrs`: the
    dict stays mutable and the model stays unhashable. Anything that needs an
    event as a set member or dict key (Task 19's coverage signatures) must hash
    a derived tuple, not the event.
    """

    model_config = {"frozen": True}

    ts: float
    kind: EventKind
    source: str
    attrs: dict[str, Any] = {}

    @field_validator("attrs")
    @classmethod
    def _normalise_attrs(cls, value: dict[str, Any]) -> dict[str, Any]:
        return {str(key): _json_native(item) for key, item in value.items()}


class Trace(BaseModel):
    model_config = {"frozen": True}

    trial_id: str
    events: list[Event] = []

    def sorted(self) -> Trace:
        return Trace(trial_id=self.trial_id, events=sorted(self.events, key=lambda e: e.ts))

    def of_kind(self, *kinds: EventKind) -> list[Event]:
        wanted = set(kinds)
        return [e for e in self.events if e.kind in wanted]

    def tool_sequence(self) -> list[str]:
        return [str(e.attrs.get("name", "?")) for e in self.of_kind("tool_call")]

    @classmethod
    def merge(cls, trial_id: str, *traces: Trace) -> Trace:
        """Interleave traces from several observers into one timeline.

        `sorted` is stable, so events sharing a timestamp keep the argument
        order of their traces. That is deliberate: it makes a merge of the same
        observers reproducible instead of dependent on dict or set ordering.
        """
        events = [e for t in traces for e in t.events]
        return cls(trial_id=trial_id, events=sorted(events, key=lambda e: e.ts))

    def to_jsonl(self) -> str:
        # allow_nan=False: a NaN or infinite timestamp would otherwise be
        # written as the non-standard `NaN`/`Infinity` tokens, which are not
        # JSON, and NaN does not even compare equal to itself on reload. Fail
        # loudly at write time instead of corrupting a run store quietly.
        return "".join(
            json.dumps(e.model_dump(), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
            for e in self.events
        )

    @classmethod
    def from_jsonl(cls, text: str, trial_id: str) -> Trace:
        events = [Event.model_validate(json.loads(line)) for line in text.splitlines() if line]
        return cls(trial_id=trial_id, events=events)


def _spans_of(trace: Trace) -> list[dict[str, Any]]:
    """Build the span list: the root span first, then one span per event.

    Span construction lives here, in one place, so the shape the tests assert
    on is the same shape the collector receives. Two parallel builders would
    drift, and the one with the tests would be the one that is wrong.
    """
    ordered = trace.sorted()
    first = ordered.events[0].ts if ordered.events else 0.0
    last = ordered.events[-1].ts if ordered.events else 0.0
    root: dict[str, Any] = {
        "name": "reprobe.trial",
        "attributes": {
            "reprobe.trial_id": trace.trial_id,
            "reprobe.event_count": len(ordered.events),
        },
        "start": first,
        "end": last,
    }
    spans = [root]
    for event in ordered.events:
        attributes: dict[str, Any] = {"reprobe.source": event.source}
        for key, value in event.attrs.items():
            attributes[f"reprobe.{key}"] = value
        spans.append(
            {
                "name": f"reprobe.{event.kind}",
                "attributes": attributes,
                "start": event.ts,
                "end": event.ts,
            }
        )
    return spans


def export_otel(trace: Trace, *, endpoint: str | None) -> None:
    """Emit the trace as OTel spans to an OTLP/HTTP endpoint.

    `endpoint` is the full signal URL, including the path — the exporter uses
    what it is given verbatim and appends `/v1/traces` only when it is
    configured with no endpoint at all. So pass
    `http://localhost:4318/v1/traces`, not `http://localhost:4318`.

    No endpoint means nothing is imported, no tracer provider is created, and
    no socket is opened: installing a global provider as a side effect of
    running a trial would be rude to an embedding application.
    """
    if endpoint is None:
        return
    _emit_otlp(_spans_of(trace), endpoint)


def _ns(seconds: float) -> int:
    """Seconds to the integer nanoseconds OTel timestamps are expressed in."""
    return round(seconds * 1_000_000_000)


def _otel_safe(value: Any) -> Any | None:
    """Return `value` if OTel can carry it as an attribute, else None.

    OTel takes scalars and homogeneous sequences of scalars. A `tool_call`'s
    `input` is a nested dict, which it cannot, so it is dropped from the span
    rather than left to raise or warn in the middle of an export. The JSONL
    trace keeps the full value; the span view is lossy on purpose.
    """
    if isinstance(value, _OTEL_SCALARS):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        homogeneous = bool(items) and all(isinstance(item, type(items[0])) for item in items)
        if homogeneous and isinstance(items[0], _OTEL_SCALARS):
            return items
        return None
    return None


def _emit_otlp(
    spans: list[dict[str, Any]],
    endpoint: str | None,
    provider: Any | None = None,
) -> None:
    """Send pre-built spans through the OTel SDK.

    `provider` is a seam: tests pass a provider wired to an in-memory exporter
    so the real span builder is exercised without a socket. In production it is
    None and an OTLP/HTTP provider is built here and shut down on the way out.
    """
    from opentelemetry import trace as otel

    owned = provider is None
    if provider is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

    try:
        tracer = otel.get_tracer("reprobe", tracer_provider=provider)
        root, children = spans[0], spans[1:]
        with tracer.start_as_current_span(
            root["name"], start_time=_ns(root["start"]), end_on_exit=False
        ) as parent:
            _set_attributes(parent, root["attributes"])
            for spec in children:
                # Explicit start/end: without them the SDK stamps the wall
                # clock and every span collapses to a microsecond at export
                # time, which throws away the trial timeline the export exists
                # to show.
                child = tracer.start_span(spec["name"], start_time=_ns(spec["start"]))
                _set_attributes(child, spec["attributes"])
                child.end(end_time=_ns(spec["end"]))
        parent.end(end_time=_ns(root["end"]))
    finally:
        if owned:
            provider.shutdown()


def _set_attributes(span: Any, attributes: Mapping[str, Any]) -> None:
    for key, value in attributes.items():
        safe = _otel_safe(value)
        if safe is not None:
            span.set_attribute(key, safe)
