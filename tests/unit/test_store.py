import json
import re
import threading

import pytest

from reprobe.store import RunStore

# ------------------------------------------------------------------- open_run


def test_open_run_creates_directory_and_meta(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.open_run({"scenario": "minimal"})
    assert (tmp_path / run_id / "meta.json").exists()
    assert store.meta(run_id)["scenario"] == "minimal"


def test_meta_records_the_run_id_and_open_time(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    meta = store.meta(run_id)
    assert meta["run_id"] == run_id
    assert meta["opened_at"] > 0


def test_caller_metadata_cannot_clobber_the_run_id(tmp_path):
    """A scenario-supplied field must not be able to rewrite the store's own
    bookkeeping, or a run would be unfindable by the id it was created under."""
    store = RunStore(tmp_path)
    run_id = store.open_run({"run_id": "something-else"})
    assert store.meta(run_id)["run_id"] == run_id


def test_run_ids_are_unique_and_sortable(tmp_path):
    store = RunStore(tmp_path)
    ids = [store.open_run({}) for _ in range(20)]
    assert len(set(ids)) == 20
    assert ids == sorted(ids), "ids must sort chronologically"


def test_meta_of_unknown_run_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        RunStore(tmp_path).meta("nope")


def test_store_creates_its_root_if_absent(tmp_path):
    store = RunStore(tmp_path / "deep" / "nested")
    run_id = store.open_run({})
    assert (tmp_path / "deep" / "nested" / run_id).is_dir()


# --------------------------------------------------------------- append / read


def test_append_and_read_round_trip(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    store.append(run_id, "trials", {"i": 1})
    store.append(run_id, "trials", {"i": 2})
    assert [r["i"] for r in store.read(run_id, "trials")] == [1, 2]


def test_read_of_unknown_kind_is_empty_not_an_error(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    assert list(store.read(run_id, "findings")) == []


def test_kinds_are_separate_files(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    store.append(run_id, "trials", {"a": 1})
    store.append(run_id, "findings", {"b": 2})
    assert [r["a"] for r in store.read(run_id, "trials")] == [1]
    assert [r["b"] for r in store.read(run_id, "findings")] == [2]


def test_append_survives_non_ascii_and_newlines_in_values(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    store.append(run_id, "trials", {"text": "líne1\nline2\t✓"})
    assert next(iter(store.read(run_id, "trials")))["text"] == "líne1\nline2\t✓"


def test_append_handles_an_adversarial_payload_verbatim(tmp_path):
    """Records hold attacker-controlled text. It must round-trip byte-exact."""
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    nasty = {"payload": '}\n{"fake": true}\n​\u0000-ish \\" quote'}
    store.append(run_id, "trials", nasty)
    assert list(store.read(run_id, "trials")) == [nasty]


def test_read_tolerates_a_torn_final_line(tmp_path):
    """Runs are killed by budget caps and Ctrl-C mid-write. Triage must still
    work on what was flushed, or an interrupted run becomes unreadable."""
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    store.append(run_id, "trials", {"i": 1})
    with (tmp_path / run_id / "trials.jsonl").open("a") as fh:
        fh.write('{"i": 2, "incompl')
    assert [r["i"] for r in store.read(run_id, "trials")] == [1]


def test_read_raises_on_corruption_that_is_not_the_final_line(tmp_path):
    """A bad line in the middle is real corruption, not a torn write, and
    silently skipping it would hide lost trials."""
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    path = tmp_path / run_id / "trials.jsonl"
    path.write_text('{"i": 1}\nNOT JSON\n{"i": 3}\n')
    with pytest.raises(ValueError, match=re.escape("trials.jsonl")):
        list(store.read(run_id, "trials"))


def test_blank_lines_are_ignored(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    (tmp_path / run_id / "trials.jsonl").write_text('{"i": 1}\n\n\n{"i": 2}\n')
    assert [r["i"] for r in store.read(run_id, "trials")] == [1, 2]


def test_read_streams_rather_than_loading_everything(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    for i in range(500):
        store.append(run_id, "trials", {"i": i})
    first = next(iter(store.read(run_id, "trials")))
    assert first["i"] == 0


# --------------------------------------------------------------------- blobs


def test_blobs_are_content_addressed_and_deduplicated(tmp_path):
    store = RunStore(tmp_path)
    first = store.put_blob(b"hello")
    second = store.put_blob(b"hello")
    assert first == second
    assert store.get_blob(first) == b"hello"
    assert len(list((tmp_path / "blobs").iterdir())) == 1


def test_different_blobs_get_different_refs(tmp_path):
    store = RunStore(tmp_path)
    assert store.put_blob(b"a") != store.put_blob(b"b")


def test_blob_round_trips_binary_and_empty_data(tmp_path):
    store = RunStore(tmp_path)
    for data in (b"", bytes(range(256)), b"\x00" * 1024):
        assert store.get_blob(store.put_blob(data)) == data


def test_get_missing_blob_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        RunStore(tmp_path).get_blob("blob:deadbeefdeadbeef")


def test_get_blob_rejects_a_malformed_ref(tmp_path):
    store = RunStore(tmp_path)
    for bad in ("", "not-a-ref", "blob:", "blob:../escape", "blob:zz!!"):
        with pytest.raises(ValueError, match="blob ref"):
            store.get_blob(bad)


def test_blob_refs_cannot_escape_the_blob_directory(tmp_path):
    """Refs reach this from stored records. A traversal would let a crafted run
    read arbitrary host files during triage."""
    store = RunStore(tmp_path)
    (tmp_path / "secret.txt").write_bytes(b"host secret")
    with pytest.raises(ValueError):
        store.get_blob("blob:../secret.txt")


def test_has_blob_reports_presence_without_reading(tmp_path):
    store = RunStore(tmp_path)
    ref = store.put_blob(b"x")
    assert store.has_blob(ref)
    assert not store.has_blob("blob:deadbeefdeadbeef")


# ----------------------------------------------------------------- latest_run


def test_latest_run_returns_most_recently_opened(tmp_path):
    store = RunStore(tmp_path)
    store.open_run({})
    second = store.open_run({})
    assert store.latest_run() == second


def test_latest_run_is_correct_for_runs_opened_in_the_same_second(tmp_path):
    """`reprobe triage` defaults to the latest run. Second-granularity ids tie,
    and a tie-break on a random suffix would silently triage the wrong run."""
    store = RunStore(tmp_path)
    ids = [store.open_run({}) for _ in range(10)]
    assert store.latest_run() == ids[-1]


def test_latest_run_raises_when_there_are_no_runs(tmp_path):
    with pytest.raises(FileNotFoundError, match="no runs"):
        RunStore(tmp_path).latest_run()


def test_latest_run_ignores_the_blobs_directory(tmp_path):
    store = RunStore(tmp_path)
    store.put_blob(b"x")
    run_id = store.open_run({})
    assert store.latest_run() == run_id


def test_runs_lists_every_run_newest_first(tmp_path):
    store = RunStore(tmp_path)
    ids = [store.open_run({}) for _ in range(3)]
    assert store.runs() == list(reversed(ids))


# ---------------------------------------------------------------- concurrency


def test_concurrent_appends_do_not_corrupt_the_file(tmp_path):
    """The search loop records trials from a thread pool, so every record must
    survive with its content intact.

    Measured honestly: unsynchronised `O_APPEND` writes stayed intact here up to
    512 KB records, so this pins the invariant rather than demonstrating a bug
    the lock fixes. It would catch a regression that buffers or batches writes,
    and the lock is justified by NFS and volume-mount cases this test cannot
    reach -- see the module docstring."""
    store = RunStore(tmp_path)
    run_id = store.open_run({})
    big = "x" * 8000  # comfortably past any atomic-append guarantee

    def worker(worker_id: int) -> None:
        for i in range(25):
            store.append(run_id, "trials", {"w": worker_id, "i": i, "pad": big})

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records = list(store.read(run_id, "trials"))
    assert len(records) == 200
    assert {(r["w"], r["i"]) for r in records} == {(w, i) for w in range(8) for i in range(25)}
    assert all(r["pad"] == big for r in records)


def test_concurrent_put_blob_of_the_same_content_is_safe(tmp_path):
    """Identical traces are common, so many threads store the same blob at once.

    This race is real and reproducible: with a temp path shared between writers,
    one writer's `replace()` pulls the file out from under another and the other
    raises FileNotFoundError mid-write. Measured 8 of 12 writers failing that
    way, so the assertions below count completions and surface thread
    exceptions -- asserting only `len(set(refs)) == 1` would pass even when two
    thirds of the writers had crashed."""
    store = RunStore(tmp_path)
    data = b"y" * 300_000
    refs: list[str] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            ref = store.put_blob(data)
        except BaseException as exc:  # recorded, then asserted on in the main thread
            with lock:
                failures.append(exc)
            return
        with lock:
            refs.append(ref)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not failures, f"writers raised: {failures!r}"
    assert len(refs) == 12, "every writer must complete"
    assert len(set(refs)) == 1, "same content, one ref"
    assert store.get_blob(refs[0]) == data
    assert not list((tmp_path / "blobs").glob("*.tmp*")), "no temp files left behind"


def test_concurrent_open_run_yields_distinct_runs(tmp_path):
    store = RunStore(tmp_path)
    ids: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        run_id = store.open_run({})
        with lock:
            ids.append(run_id)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(ids)) == 16


# ------------------------------------------------------------------ durability


def test_records_are_readable_by_a_second_store_instance(tmp_path):
    """Nothing is cached in memory: `reprobe triage` is a separate process from
    `reprobe fuzz`, and must see everything the fuzz run wrote."""
    writer = RunStore(tmp_path)
    run_id = writer.open_run({"scenario": "minimal"})
    writer.append(run_id, "trials", {"i": 1})
    ref = writer.put_blob(b"trace bytes")

    reader = RunStore(tmp_path)
    assert reader.meta(run_id)["scenario"] == "minimal"
    assert [r["i"] for r in reader.read(run_id, "trials")] == [1]
    assert reader.get_blob(ref) == b"trace bytes"
    assert reader.latest_run() == run_id


def test_meta_is_valid_json_on_disk(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.open_run({"caps": {"max_usd": 1.0}})
    raw = json.loads((tmp_path / run_id / "meta.json").read_text())
    assert raw["caps"]["max_usd"] == 1.0


def test_runs_is_empty_if_the_root_disappears(tmp_path):
    """The store creates its root at construction, but a run directory can be
    removed underneath it — a cleared volume mount, a tidied temp dir. Listing
    should come back empty rather than raising."""
    import shutil

    store = RunStore(tmp_path / "root")
    store.open_run({})
    shutil.rmtree(tmp_path / "root")
    assert store.runs() == []
    with pytest.raises(FileNotFoundError, match="no runs"):
        store.latest_run()


def test_run_ids_sort_chronologically_across_a_second_boundary(tmp_path, monkeypatch):
    """`open_run` must read the clock once, not twice.

    It built the id from a `strftime` call and a separate `time.time()` call.
    When the second boundary falls between them the stamp stays in the old
    second while the microseconds wrap to near zero, so that id sorts *earlier*
    than ids minted earlier in the same second -- and `latest_run` returns the
    wrong run. `reprobe triage` defaults to the latest run, so this silently
    triages the wrong one. Observed as a CI flake before it was understood.
    """
    import time

    from reprobe import store as store_mod

    # Captured before patching: `store_mod.time` *is* the time module, so
    # patching it would otherwise make these recurse.
    real_strftime, real_gmtime = time.strftime, time.gmtime

    base = 1_800_000_000.0  # a whole second, so the boundary is exactly at +1.0
    state = {"now": base + 0.5}

    def fake_time():
        return state["now"]

    def fake_strftime(fmt, when=None):
        # Calling it with no argument is the bug: the caller re-reads the clock
        # instead of formatting the instant it already has. Pin that read to the
        # earlier second, which makes the split deterministic instead of a race.
        return real_strftime(fmt, when if when is not None else real_gmtime(base))

    monkeypatch.setattr(store_mod.time, "time", fake_time)
    monkeypatch.setattr(store_mod.time, "strftime", fake_strftime)
    monkeypatch.setattr(store_mod.time, "localtime", real_gmtime)

    store = RunStore(tmp_path)
    first = store.open_run({})
    state["now"] = base + 1.000001  # the next second
    second = store.open_run({})

    assert sorted([first, second]) == [first, second], (first, second)
    assert store.latest_run() == second
