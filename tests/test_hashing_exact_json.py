from __future__ import annotations

import json
import math
from pathlib import Path

from wikidisputes_ssot.exact_json import Span, array_items, object_members, value_end
from wikidisputes_ssot.hashing import canonical_json_bytes, projection_hash, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]


def test_exact_json_spans_preserve_whitespace_escapes_and_markup() -> None:
    raw = b' [ { "text" : " x\\n&lt;b&gt;\\u2603 ", "extra": [1, null] } ]\n'
    span = Span(1, value_end(raw, 1))
    item = next(array_items(raw, span))
    exact = raw[item.start : item.end]
    assert exact == b'{ "text" : " x\\n&lt;b&gt;\\u2603 ", "extra": [1, null] }'
    members = dict(object_members(raw, item))
    assert json.loads(raw[members[b"text"].start : members[b"text"].end]) == " x\n&lt;b&gt;☃ "


def test_nonfinite_float_is_portably_tagged() -> None:
    encoded = canonical_json_bytes({"toxicity": math.nan})
    assert encoded == b'{"toxicity":{"$wikidisputes_nonfinite_float":"NaN"}}'


def test_projection_hash_is_ordered_and_content_sensitive() -> None:
    fields = ("lineage", "text", "value")
    first = projection_hash({"lineage": "x", "text": " a ", "value": None}, fields)
    same = projection_hash({"value": None, "text": " a ", "lineage": "x"}, fields)
    changed = projection_hash({"lineage": "x", "text": "a", "value": None}, fields)
    assert first == same
    assert first != changed
    assert len(sha256_bytes(b"fixture")) == 64


def test_committed_projection_serialization_vectors() -> None:
    vectors = json.loads(
        (ROOT / "schemas" / "source_projection_test_vectors.json").read_text(encoding="utf-8")
    )
    fields = tuple(vectors["field_order"])
    for vector in vectors["vectors"]:
        assert projection_hash(vector["fields"], fields) == vector["sha256"]
