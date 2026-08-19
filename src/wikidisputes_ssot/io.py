from __future__ import annotations

import gzip
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .hashing import sha256_bytes, sha256_file


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def atomic_parquet(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    try:
        pq.write_table(
            table,
            temporary,
            compression="zstd",
            compression_level=9,
            use_dictionary=True,
            write_statistics=True,
            data_page_version="2.0",
        )
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def atomic_link_or_copy(source: Path, target: Path) -> None:
    """Atomically mirror an immutable artifact, using no extra blocks when possible."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        temporary_path.unlink()
        try:
            os.link(source, temporary_path)
        except OSError:
            shutil.copyfile(source, temporary_path)
        os.replace(temporary_path, target)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def write_rows(path: Path, rows: Iterable[Mapping[str, Any]], schema: pa.Schema) -> None:
    atomic_parquet(path, pa.Table.from_pylist(list(rows), schema=schema))


def table_from_union_pylist(rows: Iterable[Mapping[str, Any]]) -> pa.Table:
    """Build a table without losing fields that occur after the first record.

    ``Table.from_pylist`` derives mapping field names from the first item.  That
    is unsafe for event/display unions whose optional fields depend on row kind.
    Normalize every mapping to the ordered union before asking Arrow to infer
    value types across the complete collection.
    """
    materialized = list(rows)
    if not materialized:
        return pa.table({"_empty": pa.array([], pa.string())})
    columns = list(dict.fromkeys(key for row in materialized for key in row))
    return pa.Table.from_pylist(
        [{column: row.get(column) for column in columns} for row in materialized]
    )


def file_descriptor(path: Path) -> dict[str, Any]:
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, content: bytes, suffix: str = ".blob") -> dict[str, Any]:
        digest = sha256_bytes(content)
        relative = Path("sha256") / digest[:2] / f"{digest}{suffix}"
        target = self.root / relative
        if not target.exists():
            atomic_write_bytes(target, content)
        elif sha256_file(target) != digest:
            raise RuntimeError(f"content-addressed blob collision/corruption: {target}")
        return {"blob_sha256": digest, "blob_bytes": len(content), "blob_path": str(relative)}

    def put_gzip(self, content: bytes, suffix: str = ".gz") -> dict[str, Any]:
        """Store a deterministic compressed envelope while hashing exact content too."""
        compressed = gzip.compress(content, compresslevel=9, mtime=0)
        descriptor = self.put(compressed, suffix=suffix)
        return {
            **descriptor,
            "content_sha256": sha256_bytes(content),
            "content_bytes": len(content),
            "storage_encoding": "gzip-mtime-0",
        }
