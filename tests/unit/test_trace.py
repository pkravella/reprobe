from reprobe.trace import Event, Trace


def _trace():
    return Trace(
        trial_id="trial_1",
        events=[
            Event(ts=2.0, kind="file_read", source="strace", attrs={"path": "/w/.env"}),
            Event(ts=1.0, kind="tool_call", source="agent", attrs={"name": "Read"}),
            Event(
                ts=3.0,
                kind="net_attempt",
                source="gateway",
                attrs={"host": "evil.test", "body": "x"},
            ),
        ],
    )


def test_sorted_orders_by_timestamp():
    assert [e.ts for e in _trace().sorted().events] == [1.0, 2.0, 3.0]


def test_sorted_does_not_mutate_the_original():
    original = _trace()
    original.sorted()
    assert [e.ts for e in original.events] == [2.0, 1.0, 3.0]


def test_of_kind_filters_and_preserves_order():
    events = _trace().sorted().of_kind("file_read", "net_attempt")
    assert [e.kind for e in events] == ["file_read", "net_attempt"]


def test_of_kind_with_no_kinds_is_empty():
    assert _trace().of_kind() == []


def test_merge_combines_and_sorts():
    merged = Trace.merge("trial_1", _trace(), _trace())
    assert len(merged.events) == 6
    assert [e.ts for e in merged.events] == [1.0, 1.0, 2.0, 2.0, 3.0, 3.0]
    assert merged.trial_id == "trial_1"


def test_merge_of_nothing_is_an_empty_trace():
    assert Trace.merge("trial_1") == Trace(trial_id="trial_1", events=[])


def test_merge_is_stable_across_sources_at_the_same_timestamp():
    a = Trace(trial_id="t", events=[Event(ts=1.0, kind="file_write", source="fsdiff", attrs={})])
    b = Trace(trial_id="t", events=[Event(ts=1.0, kind="file_write", source="strace", attrs={})])
    assert [e.source for e in Trace.merge("t", a, b).events] == ["fsdiff", "strace"]


def test_jsonl_round_trip_is_lossless():
    original = _trace().sorted()
    restored = Trace.from_jsonl(original.to_jsonl(), trial_id="trial_1")
    assert restored == original


def test_jsonl_round_trip_survives_non_json_native_attrs():
    # A strace-derived process_exec event carries argv. If attrs are stored as a
    # tuple, JSON reloads them as a list and the round trip silently loses
    # equality, so attrs are normalised to JSON-native types on construction.
    original = Trace(
        trial_id="t",
        events=[
            Event(ts=1.0, kind="process_exec", source="strace", attrs={"argv": ("curl", "-X")})
        ],
    )
    assert original.events[0].attrs["argv"] == ["curl", "-X"]
    assert Trace.from_jsonl(original.to_jsonl(), trial_id="t") == original


def test_jsonl_round_trip_survives_unicode_and_newlines():
    original = Trace(
        trial_id="t",
        events=[Event(ts=1.0, kind="agent_message", source="agent", attrs={"text": "café\nline"})],
    )
    assert Trace.from_jsonl(original.to_jsonl(), trial_id="t") == original


def test_to_jsonl_emits_one_line_per_event():
    assert len(_trace().to_jsonl().splitlines()) == 3


def test_from_jsonl_ignores_blank_lines():
    text = _trace().sorted().to_jsonl()
    assert Trace.from_jsonl("\n" + text + "\n", trial_id="trial_1") == _trace().sorted()


def test_empty_trace_round_trips():
    empty = Trace(trial_id="t")
    assert empty.to_jsonl() == ""
    assert Trace.from_jsonl("", trial_id="t") == empty


def test_tool_sequence_is_tool_names_in_order():
    t = Trace(
        trial_id="t",
        events=[
            Event(ts=1.0, kind="tool_call", source="agent", attrs={"name": "Read"}),
            Event(ts=2.0, kind="file_read", source="strace", attrs={"path": "/x"}),
            Event(ts=3.0, kind="tool_call", source="agent", attrs={"name": "Bash"}),
        ],
    )
    assert t.tool_sequence() == ["Read", "Bash"]


def test_tool_sequence_marks_an_unnamed_tool_call():
    t = Trace(trial_id="t", events=[Event(ts=1.0, kind="tool_call", source="agent", attrs={})])
    assert t.tool_sequence() == ["?"]


def test_an_unknown_event_kind_is_rejected():
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        Event(ts=1.0, kind="telepathy", source="agent", attrs={})


# --- OTel export -----------------------------------------------------------


def test_export_otel_is_a_noop_without_an_endpoint(monkeypatch):
    from reprobe import trace as trace_mod

    def _fail(*args, **kwargs):
        raise AssertionError("no endpoint means no emitter must be invoked")

    monkeypatch.setattr(trace_mod, "_emit_otlp", _fail)
    trace_mod.export_otel(_trace(), endpoint=None)


def test_export_otel_emits_one_span_per_event(monkeypatch):
    from reprobe import trace as trace_mod

    captured = []
    monkeypatch.setattr(trace_mod, "_emit_otlp", lambda spans, endpoint: captured.extend(spans))
    trace_mod.export_otel(_trace(), endpoint="http://localhost:4318/v1/traces")
    # root span + three event spans
    assert len(captured) == 4
    assert captured[0]["name"] == "reprobe.trial"
    assert [s["name"] for s in captured[1:]] == [
        "reprobe.tool_call",
        "reprobe.file_read",
        "reprobe.net_attempt",
    ]


def test_export_otel_root_span_covers_the_whole_trial(monkeypatch):
    from reprobe import trace as trace_mod

    captured = []
    monkeypatch.setattr(trace_mod, "_emit_otlp", lambda spans, endpoint: captured.extend(spans))
    trace_mod.export_otel(_trace(), endpoint="http://x/v1/traces")
    root = captured[0]
    assert (root["start"], root["end"]) == (1.0, 3.0)
    assert root["attributes"]["reprobe.trial_id"] == "trial_1"
    assert root["attributes"]["reprobe.event_count"] == 3


def test_export_otel_event_spans_carry_event_timestamps(monkeypatch):
    from reprobe import trace as trace_mod

    captured = []
    monkeypatch.setattr(trace_mod, "_emit_otlp", lambda spans, endpoint: captured.extend(spans))
    trace_mod.export_otel(_trace(), endpoint="http://x/v1/traces")
    assert [(s["start"], s["end"]) for s in captured[1:]] == [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]


def test_export_otel_prefixes_attrs_and_records_the_source(monkeypatch):
    from reprobe import trace as trace_mod

    captured = []
    monkeypatch.setattr(trace_mod, "_emit_otlp", lambda spans, endpoint: captured.extend(spans))
    trace_mod.export_otel(_trace(), endpoint="http://x/v1/traces")
    net = captured[3]
    assert net["attributes"] == {
        "reprobe.source": "gateway",
        "reprobe.host": "evil.test",
        "reprobe.body": "x",
    }


def test_export_otel_of_an_empty_trace_emits_only_a_root_span(monkeypatch):
    from reprobe import trace as trace_mod

    captured = []
    monkeypatch.setattr(trace_mod, "_emit_otlp", lambda spans, endpoint: captured.extend(spans))
    trace_mod.export_otel(Trace(trial_id="t"), endpoint="http://x/v1/traces")
    assert len(captured) == 1
    assert (captured[0]["start"], captured[0]["end"]) == (0.0, 0.0)


def test_emit_otlp_builds_real_spans_with_event_timestamps():
    # Exercises the real OTel span builder offline, through an in-memory
    # exporter. The plan built spans with `start_as_current_span` and no
    # `start_time`, which stamps every span with the wall clock instead of the
    # event's ts and throws the trial timeline away.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from reprobe import trace as trace_mod

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))

    trace_mod._emit_otlp(trace_mod._spans_of(_trace()), endpoint=None, provider=provider)

    spans = memory.get_finished_spans()
    assert [s.name for s in spans] == [
        "reprobe.tool_call",
        "reprobe.file_read",
        "reprobe.net_attempt",
        "reprobe.trial",
    ]
    by_name = {s.name: s for s in spans}
    assert by_name["reprobe.file_read"].start_time == 2_000_000_000
    assert by_name["reprobe.file_read"].end_time == 2_000_000_000
    assert by_name["reprobe.trial"].start_time == 1_000_000_000
    assert by_name["reprobe.trial"].end_time == 3_000_000_000
    assert by_name["reprobe.file_read"].attributes["reprobe.path"] == "/w/.env"
    assert by_name["reprobe.trial"].attributes["reprobe.event_count"] == 3


def test_emit_otlp_nests_event_spans_under_the_root():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from reprobe import trace as trace_mod

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))

    trace_mod._emit_otlp(trace_mod._spans_of(_trace()), endpoint=None, provider=provider)

    spans = {s.name: s for s in memory.get_finished_spans()}
    root = spans["reprobe.trial"]
    for name in ("reprobe.tool_call", "reprobe.file_read", "reprobe.net_attempt"):
        assert spans[name].parent is not None
        assert spans[name].parent.span_id == root.context.span_id
        assert spans[name].context.trace_id == root.context.trace_id


def test_emit_otlp_drops_attrs_otel_cannot_carry():
    # OTel attribute values must be scalars or homogeneous sequences of them. A
    # dict-valued attr is dropped rather than allowed to raise mid-export.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from reprobe import trace as trace_mod

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))

    trace = Trace(
        trial_id="t",
        events=[
            Event(
                ts=1.0,
                kind="tool_call",
                source="agent",
                attrs={"name": "Read", "input": {"file_path": "/x"}, "argv": ["a", "b"]},
            )
        ],
    )
    trace_mod._emit_otlp(trace_mod._spans_of(trace), endpoint=None, provider=provider)

    attrs = {s.name: s.attributes for s in memory.get_finished_spans()}["reprobe.tool_call"]
    assert attrs["reprobe.name"] == "Read"
    assert attrs["reprobe.argv"] == ("a", "b")
    assert "reprobe.input" not in attrs


def test_export_otel_passes_the_endpoint_through(monkeypatch):
    from reprobe import trace as trace_mod

    seen = {}

    def _capture(spans, endpoint):
        seen["endpoint"] = endpoint

    monkeypatch.setattr(trace_mod, "_emit_otlp", _capture)
    trace_mod.export_otel(_trace(), endpoint="http://collector:4318/v1/traces")
    assert seen["endpoint"] == "http://collector:4318/v1/traces"


def test_attrs_coerce_an_arbitrary_object_to_a_string():
    from pathlib import Path

    event = Event(ts=1.0, kind="file_write", source="fsdiff", attrs={"path": Path("/w/.env")})
    assert event.attrs["path"] == "/w/.env"
    trace = Trace(trial_id="t", events=[event])
    assert Trace.from_jsonl(trace.to_jsonl(), trial_id="t") == trace


def test_a_mixed_sequence_attr_is_dropped_from_a_span():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from reprobe import trace as trace_mod

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))

    trace = Trace(
        trial_id="t",
        events=[
            Event(ts=1.0, kind="process_exec", source="strace", attrs={"argv": [1, "a"], "e": []})
        ],
    )
    trace_mod._emit_otlp(trace_mod._spans_of(trace), endpoint=None, provider=provider)

    attrs = memory.get_finished_spans()[0].attributes
    assert "reprobe.argv" not in attrs
    assert "reprobe.e" not in attrs


def test_a_non_finite_timestamp_is_refused_rather_than_written_as_invalid_json():
    import pytest

    trace = Trace(trial_id="t", events=[Event(ts=float("nan"), kind="tool_call", source="agent")])
    with pytest.raises(ValueError, match="Out of range float"):
        trace.to_jsonl()


def test_emit_otlp_wires_the_endpoint_into_a_real_otlp_exporter(monkeypatch):
    # Covers the production path -- a provider it owns and shuts down -- without
    # opening a socket. The endpoint must arrive at the exporter verbatim: the
    # OTLP/HTTP exporter appends `/v1/traces` only when given no endpoint, so a
    # caller passing a bare host:port would silently POST to the collector root.
    from opentelemetry.exporter.otlp.proto.http import trace_exporter as otlp_mod

    from reprobe import trace as trace_mod

    seen = {}

    class StubExporter:
        def __init__(self, endpoint=None, **kwargs):
            seen["endpoint"] = endpoint
            seen["exported"] = []

        def export(self, spans):
            from opentelemetry.sdk.trace.export import SpanExportResult

            seen["exported"].extend(spans)
            return SpanExportResult.SUCCESS

        def force_flush(self, timeout_millis=30_000):
            return True

        def shutdown(self):
            seen["shutdown"] = True

    monkeypatch.setattr(otlp_mod, "OTLPSpanExporter", StubExporter)

    trace_mod._emit_otlp(trace_mod._spans_of(_trace()), endpoint="http://collector:4318/v1/traces")

    assert seen["endpoint"] == "http://collector:4318/v1/traces"
    assert seen["shutdown"] is True
    assert sorted(s.name for s in seen["exported"]) == [
        "reprobe.file_read",
        "reprobe.net_attempt",
        "reprobe.tool_call",
        "reprobe.trial",
    ]
