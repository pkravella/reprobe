"""Append-only run storage: the replay guarantee, on disk.

Layout::

    <root>/<run_id>/meta.json       run-level metadata (versions, caps, scenario hash)
    <root>/<run_id>/trials.jsonl    one line per trial
    <root>/<run_id>/candidates.jsonl
    <root>/<run_id>/findings.jsonl
    <root>/blobs/<hash>             content-addressed traces, payloads, raw stdout

JSONL plus content-addressed blobs, because a run is thousands of records that
must survive a crash mid-write, and because two trials producing identical
traces should not store them twice.

Three properties this has to hold, each of which has a test:

* **Safe under concurrency.** The search loop records trials and stores blobs
  from a thread pool. The blob race is real and reproducible: a temp path
  shared between writers lets one writer's `replace()` pull the file out from
  under another, which raises mid-write. Appends are locked as cheap
  insurance rather than to fix an observed bug -- measured here, concurrent
  `O_APPEND` writes stayed intact up to 512 KB records on local APFS, because
  the kernel updates the offset and writes under one inode lock. POSIX does
  not guarantee that for arbitrary sizes though, it does not hold on NFS, and
  this store is written through Docker volume mounts, so the lock stays.
* **Readable after a kill.** Runs end by hitting a budget cap or Ctrl-C, often
  mid-write. A torn final line is expected and tolerated; a malformed line
  anywhere else is real corruption and raises, because silently skipping it
  would hide lost trials.
* **Readable from another process.** `reprobe triage` is a separate invocation
  from `reprobe fuzz`. Nothing is cached in memory.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from reprobe.ids import digest_bytes, new_id

_BLOB_PREFIX = "blob"
_BLOBS_DIR = "blobs"
_META_FILE = "meta.json"
# A blob ref's body is the hex digest from `reprobe.ids`. Validated rather than
# trusted: refs arrive from stored records, and an unchecked one is a path
# traversal that would read host files during triage.
_BLOB_BODY = re.compile(r"\A[0-9a-f]{8,64}\Z")


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.blobs = self.root / _BLOBS_DIR
        self.blobs.mkdir(parents=True, exist_ok=True)
        # One lock for appends and run creation. Held only for a write that is
        # microseconds against the seconds of latency in an agent trial.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------- runs

    def open_run(self, meta: dict[str, Any]) -> str:
        """Create a new run directory and write its metadata. Returns the run id.

        The id is timestamped to microseconds so that ids sort chronologically
        even for runs opened in the same second -- `reprobe triage` defaults to
        the latest run, and a tie broken by a random suffix would silently
        triage the wrong one.

        Both halves of the stamp come from **one** clock reading. Reading it
        twice meant that when the second boundary fell between the two reads,
        the seconds stayed in the old second while the microseconds wrapped to
        near zero -- so that id sorted *earlier* than ids minted earlier in the
        same second, and `latest_run` returned the wrong run. It surfaced as a
        CI flake before it was understood.
        """
        with self._lock:
            while True:
                now = time.time()
                stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now))
                micros = f"{int(now % 1 * 1_000_000):06d}"
                run_id = f"{stamp}-{micros}-{new_id('r').split('_')[1]}"
                directory = self.root / run_id
                try:
                    directory.mkdir(parents=True)
                except FileExistsError:  # pragma: no cover - astronomically rare
                    continue
                break

        # Written after the caller's fields so the store's own bookkeeping
        # cannot be clobbered by scenario-supplied metadata.
        payload = {**meta, "run_id": run_id, "opened_at": time.time()}
        (directory / _META_FILE).write_text(json.dumps(payload, indent=2, default=str))
        return run_id

    def meta(self, run_id: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.root / run_id / _META_FILE).read_text())
        return data

    def runs(self) -> list[str]:
        """Every run id, newest first."""
        if not self.root.is_dir():
            return []
        names = [
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and p.name != _BLOBS_DIR and (p / _META_FILE).exists()
        ]
        return sorted(names, reverse=True)

    def latest_run(self) -> str:
        runs = self.runs()
        if not runs:
            raise FileNotFoundError(f"no runs in {self.root}")
        return runs[0]

    # ---------------------------------------------------------------- records

    def append(self, run_id: str, kind: str, record: dict[str, Any]) -> None:
        """Append one JSON line. Safe to call from several threads at once.

        The record is serialised before the lock is taken, so the lock covers
        only the write.
        """
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        path = self.root / run_id / f"{kind}.jsonl"
        with self._lock, path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()

    def read(self, run_id: str, kind: str) -> Iterator[dict[str, Any]]:
        """Stream records. Missing file yields nothing; see the module docstring
        for how torn and corrupt lines differ."""
        path = self.root / run_id / f"{kind}.jsonl"
        if not path.exists():
            return
        with path.open(encoding="utf-8") as fh:
            lines = fh.readlines()
        last = len(lines) - 1
        for index, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                if index == last and not raw.endswith("\n"):
                    return  # torn final write: the run was killed mid-record
                raise ValueError(
                    f"{path} line {index + 1} is not valid JSON: {exc}. "
                    f"A malformed line that is not the last one means the file "
                    f"is corrupt, and records may have been lost."
                ) from exc

    # ------------------------------------------------------------------ blobs

    def _blob_path(self, ref: str) -> Path:
        body = ref.split(":", 1)[1] if ":" in ref else ref
        if not _BLOB_BODY.match(body):
            raise ValueError(
                f"malformed blob ref {ref!r}; expected "
                f"'{_BLOB_PREFIX}:<hex digest>' as produced by put_blob"
            )
        return self.blobs / body

    def put_blob(self, data: bytes) -> str:
        """Store bytes content-addressed. Returns a ref; identical data dedupes.

        The temp file is per-writer: several threads commonly store the same
        trace at once, and a shared temp path would let one writer truncate
        another's partially written file.
        """
        ref = digest_bytes(data, prefix=_BLOB_PREFIX)
        target = self._blob_path(ref)
        if target.exists():
            return ref
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)  # atomic within a filesystem
        return ref

    def get_blob(self, ref: str) -> bytes:
        return self._blob_path(ref).read_bytes()

    def has_blob(self, ref: str) -> bool:
        return self._blob_path(ref).exists()


__all__ = ["RunStore"]
