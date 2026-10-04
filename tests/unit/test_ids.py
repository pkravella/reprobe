import json
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reprobe.ids import (
    HASH_VERSION,
    canonical_json,
    digest,
    digest_bytes,
    new_id,
)

# --------------------------------------------------------------- canonical_json


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_canonical_json_is_key_order_independent_when_nested():
    assert canonical_json({"o": {"a": 1, "b": 2}}) == canonical_json({"o": {"b": 2, "a": 1}})


def test_canonical_json_distinguishes_types():
    assert canonical_json({"a": 1}) != canonical_json({"a": "1"})
    assert canonical_json({"a": 1}) != canonical_json({"a": True})
    assert canonical_json({"a": 1}) != canonical_json({"a": 1.0})


def test_canonical_json_is_bytes_and_compact():
    out = canonical_json({"a": [1, 2]})
    assert isinstance(out, bytes)
    assert b" " not in out


def test_canonical_json_preserves_list_order():
    assert canonical_json([1, 2]) != canonical_json([2, 1])


def test_canonical_json_round_trips_as_json():
    obj = {"b": [1, {"c": None}], "a": "x"}
    assert json.loads(canonical_json(obj)) == obj


def test_canonical_json_keeps_non_ascii_as_utf8_not_escapes():
    """`ensure_ascii=False` keeps payload text compact; payloads are adversarial
    and often non-ASCII (zero-width joiners, RTL marks)."""
    text = "caf\u00e9"
    out = canonical_json({"t": text})
    assert text.encode("utf-8") in out
    assert b"\\u00e9" not in out, "must not be \\uXXXX-escaped"


def test_canonical_json_rejects_nan_and_infinity():
    """NaN and Infinity are not valid JSON. Serialising them would produce a
    hash no other JSON reader could reproduce."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_json({"x": bad})


def test_canonical_json_rejects_unserialisable_objects():
    with pytest.raises(TypeError):
        canonical_json(object())


# ---------------------------------------------------------------------- digest


def test_digest_is_stable_and_prefixed():
    first = digest({"x": 1}, prefix="scn")
    second = digest({"x": 1}, prefix="scn")
    assert first == second
    assert first.startswith("scn:")
    assert len(first.split(":")[1]) == 16


def test_digest_without_a_prefix_is_bare_hex():
    out = digest({"x": 1})
    assert re.fullmatch(r"[0-9a-f]{16}", out)


def test_digest_changes_with_content():
    assert digest({"x": 1}) != digest({"x": 2})


def test_digest_ignores_key_order():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_digest_prefix_does_not_change_the_hash_body():
    assert digest({"x": 1}, prefix="scn").split(":")[1] == digest({"x": 1})


def test_digest_rejects_unserialisable_objects():
    with pytest.raises(TypeError):
        digest(object())


# ---------------------------------------------------------------- digest_bytes


def test_digest_bytes_is_stable_and_prefixed():
    first = digest_bytes(b"hello", prefix="blob")
    assert first == digest_bytes(b"hello", prefix="blob")
    assert first.startswith("blob:")
    assert len(first.split(":")[1]) == 16


def test_digest_bytes_changes_with_content():
    assert digest_bytes(b"hello") != digest_bytes(b"hellp")


def test_digest_bytes_handles_empty_and_binary_input():
    assert re.fullmatch(r"[0-9a-f]{16}", digest_bytes(b""))
    assert re.fullmatch(r"[0-9a-f]{16}", digest_bytes(bytes(range(256))))


def test_digest_bytes_differs_from_digest_of_the_same_text():
    """They hash different things; a blob ref must never collide with an object
    hash just because the bytes happen to look like the JSON."""
    assert digest_bytes(canonical_json({"x": 1})) != digest({"x": 1})


# ----------------------------------------------------------------- hash version


def test_hash_version_participates_in_every_digest(monkeypatch):
    """The module docstring promises that bumping HASH_VERSION makes stored
    hashes cold. That is only true if the version is actually hashed."""
    import reprobe.ids as ids

    before_obj = ids.digest({"x": 1})
    before_blob = ids.digest_bytes(b"x")
    monkeypatch.setattr(ids, "HASH_VERSION", HASH_VERSION + 1)
    assert ids.digest({"x": 1}) != before_obj
    assert ids.digest_bytes(b"x") != before_blob


# ---------------------------------------------------------------------- new_id


def test_new_id_is_unique_and_prefixed():
    ids = {new_id("trial") for _ in range(1000)}
    assert len(ids) == 1000
    assert all(i.startswith("trial_") for i in ids)


def test_new_id_token_shape_is_fixed():
    kind, _, token = new_id("trial").partition("_")
    assert kind == "trial"
    assert re.fullmatch(r"[a-z2-7]{12}", token), "lowercase base32, no padding"


def test_new_id_is_not_a_content_hash():
    """new_id is for identity, digest is for content. Two ids for the same kind
    must differ, or trials would overwrite each other in the run store."""
    assert new_id("trial") != new_id("trial")


# ------------------------------------------------------------------ properties

_json_scalars = st.none() | st.booleans() | st.integers() | st.text()
_json_values = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=10,
)


@given(st.dictionaries(st.text(max_size=8), _json_values, max_size=6))
def test_digest_is_invariant_under_key_reordering(obj):
    """The invariant the whole project rests on: two values that are equal hash
    the same, however their keys happened to be ordered in memory."""
    shuffled = dict(reversed(list(obj.items())))
    assert digest(obj) == digest(shuffled)


@given(_json_values)
def test_digest_never_raises_on_json_able_input(value):
    assert re.fullmatch(r"[0-9a-f]{16}", digest(value))


@given(_json_values, _json_values)
def test_digest_is_a_faithful_function_of_canonical_json(a, b):
    """The real contract, and it is deliberately *not* stated in terms of
    Python `==`.

    Python equality is not JSON equality: `[False, False] == [False, 0]` is
    True, because `False == 0`, yet those serialise to `[false,false]` and
    `[false,0]` and must hash differently -- we want type-distinguishing
    hashes, as `test_canonical_json_distinguishes_types` asserts. So the
    property is that `digest` agrees exactly with `canonical_json`.
    """
    if canonical_json(a) == canonical_json(b):
        assert digest(a) == digest(b)
    else:
        assert digest(a) != digest(b)
