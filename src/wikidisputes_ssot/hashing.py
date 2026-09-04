from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def hash_stream(handle: BinaryIO, chunk_size: int = 8 * 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    while chunk := handle.read(chunk_size):
        digest.update(chunk)
        length += len(chunk)
    return digest.hexdigest(), length


def portable_json_value(value: Any) -> Any:
    """Map non-standard JSON floats to explicit tagged values.

    Python's JSON decoder accepts the release's bare NaN tokens. The exact bytes
    remain authoritative; this mapping is only for a portable derived canonical
    representation that cannot silently collapse NaN, infinity, or null.
    """
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            label = "NaN"
        elif value > 0:
            label = "Infinity"
        else:
            label = "-Infinity"
        return {"$wikidisputes_nonfinite_float": label}
    if isinstance(value, dict):
        return {str(key): portable_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [portable_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [portable_json_value(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        portable_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_hash(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def iter_file_chunks(path: Path, chunk_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            yield chunk


def projection_hash(fields: Mapping[str, Any], ordered_fields: tuple[str, ...]) -> str:
    """Hash a versioned, explicitly ordered immutable projection.

    The serialized value is a JSON array. This makes field order explicit while
    preserving JSON's distinct string/number/boolean/null types and UTF-8 bytes.
    """
    return canonical_json_hash([fields.get(name) for name in ordered_fields])
