from reprobe.observers.fsdiff import FsDiffObserver
from reprobe.sandbox import FsDiff


def _events(diff: FsDiff, ts: float = 100.0):
    return FsDiffObserver(diff, ts=ts).events()


def test_emits_one_file_write_per_touched_path():
    diff = FsDiff(created=["A.md"], modified=["B.md"], deleted=["C.md"])
    writes = {e.attrs["path"] for e in _events(diff) if e.kind == "file_write"}
    assert writes == {"A.md", "B.md", "C.md"}


def test_records_which_operation_happened():
    diff = FsDiff(created=["A.md"], modified=["B.md"], deleted=["C.md"])
    ops = {e.attrs["path"]: e.attrs["op"] for e in _events(diff) if e.kind == "file_write"}
    assert ops == {"A.md": "created", "B.md": "modified", "C.md": "deleted"}


def test_every_event_is_attributed_to_fsdiff_at_the_given_timestamp():
    events = _events(FsDiff(created=["A.md"]), ts=42.0)
    assert all(e.source == "fsdiff" for e in events)
    assert all(e.ts == 42.0 for e in events)


def test_flags_agent_state_writes_as_memory_writes_too():
    # A write to an agent-state file is BOTH a file_write and a memory_write:
    # it changed the tree (protected_write reads this) and it changes how the
    # agent behaves on a later turn (R7 coverage).
    diff = FsDiff(modified=["CLAUDE.md", "src/app.js"])
    events = _events(diff)
    memory = [e.attrs["path"] for e in events if e.kind == "memory_write"]
    assert memory == ["CLAUDE.md"]
    writes = [e.attrs["path"] for e in events if e.kind == "file_write"]
    assert set(writes) == {"CLAUDE.md", "src/app.js"}


def test_deleting_agent_state_is_a_memory_write():
    # Removing CLAUDE.md changes later behaviour just as writing it does.
    events = _events(FsDiff(deleted=["CLAUDE.md"]))
    assert any(e.kind == "memory_write" and e.attrs["op"] == "deleted" for e in events)


def test_empty_diff_yields_no_events():
    assert _events(FsDiff()) == []


def test_events_round_trip_through_the_trace_jsonl():
    from reprobe.trace import Trace

    diff = FsDiff(created=["A.md"], modified=[".mcp.json"], deleted=["old.py"])
    trace = Trace(trial_id="t", events=_events(diff))
    assert Trace.from_jsonl(trace.to_jsonl(), trial_id="t") == trace
