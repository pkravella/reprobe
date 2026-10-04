"""One hashing algorithm for the whole project.

Everything downstream depends on this module agreeing with itself across
machines and across time: scenario hashes, prompt hashes, dedupe keys,
confirmation cache keys, and content-addressed blob refs. A finding exported on
Monday must match the finding verified on Tuesday, so there is exactly one way
to turn a value into bytes and exactly one way to turn bytes into a hash.

Changing anything here is a breaking change: exported tests pinned to an old
scenario hash will no longer match. If it must change, bump `HASH_VERSION` --
which is itself hashed, so every stored hash goes cold at once rather than some
of them silently colliding with the new scheme.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

HASH_VERSION = 1
_DIGEST_CHARS = 16
_TOKEN_CHARS = 12
_TOKEN_BYTES = 8


def canonical_json(obj: Any) -> bytes:
    """Serialise `obj` so that equal values always produce equal bytes.

    Sorted keys, no insignificant whitespace, UTF-8. Two deliberate choices:

    * `default=str` is NOT used. An object we cannot serialise is a bug we want
      to see, not something to coerce into a string that happens to hash.
    * `allow_nan=False`. NaN and Infinity are not valid JSON, so emitting them
      would produce a hash no other JSON reader could reproduce.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _hash(payload: bytes, prefix: str) -> str:
    # The version is hashed, not just documented, so bumping it really does
    # invalidate every previously stored hash. See the module docstring.
    versioned = b"reprobe/v%d\x00%b" % (HASH_VERSION, payload)
    digest_hex = hashlib.sha256(versioned).hexdigest()[:_DIGEST_CHARS]
    return f"{prefix}:{digest_hex}" if prefix else digest_hex


def digest(obj: Any, *, prefix: str = "") -> str:
    """Short, stable content hash of a JSON-serialisable value.

    `prefix` namespaces it for readability in logs and exported tests, e.g.
    `scn:1f2e3d4c5b6a7988`. The prefix does not change the hash body.
    """
    return _hash(canonical_json(obj), prefix)


def digest_bytes(data: bytes, *, prefix: str = "") -> str:
    """Content hash of raw bytes, for blobs: traces, payloads, fixture files.

    Domain-separated from `digest` so a blob ref can never collide with an
    object hash merely because the bytes happen to look like the JSON.
    """
    return _hash(b"bytes\x00" + data, prefix)


def new_id(kind: str) -> str:
    """Random, collision-resistant id with a human-readable kind prefix.

    Identity, not content: two calls never agree. Use `digest` when two equal
    values must produce the same name.
    """
    token = (
        base64.b32encode(os.urandom(_TOKEN_BYTES))
        .decode("ascii")
        .rstrip("=")
        .lower()[:_TOKEN_CHARS]
    )
    return f"{kind}_{token}"


__all__ = [
    "HASH_VERSION",
    "canonical_json",
    "digest",
    "digest_bytes",
    "new_id",
]
