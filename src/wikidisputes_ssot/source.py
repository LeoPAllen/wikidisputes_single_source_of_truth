from __future__ import annotations

import json
import mmap
import os
import shutil
import tarfile
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from .constants import (
    CURRENT,
    EXPECTED_COUNTS,
    PROJECTION_SERIALIZATION_VERSION,
    SCHEMA_VERSION,
    SourcePin,
)
from .exact_json import array_items, member_span, top_level_array_span
from .hashing import (
    canonical_json_bytes,
    canonical_json_hash,
    projection_hash,
    sha256_bytes,
    sha256_file,
)
from .io import atomic_write_json, file_descriptor

PROJECTION_FIELDS = (
    "source_repository",
    "source_commit",
    "archive_name",
    "archive_sha256",
    "archive_member_path",
    "source_file_sha256",
    "source_side",
    "source_case_index",
    "source_row_index",
    "source_record_sha256",
    "source_record_json_exact",
)


SOURCE_SCHEMA = pa.schema(
    [
        ("source_row_uid", pa.string()),
        ("source_file_uid", pa.string()),
        ("source_repository", pa.string()),
        ("source_commit", pa.string()),
        ("archive_name", pa.string()),
        ("archive_sha256", pa.string()),
        ("archive_member_path", pa.string()),
        ("source_file_sha256", pa.string()),
        ("source_side", pa.string()),
        ("source_wikidisputes_escalated", pa.bool_()),
        ("source_case_index", pa.int64()),
        ("source_row_index", pa.int64()),
        ("source_order", pa.int64()),
        ("source_case_uid", pa.string()),
        ("source_dispute_id_exact", pa.string()),
        ("source_dispute_json_canonical", pa.large_string()),
        ("source_case_offset", pa.int64()),
        ("source_case_length", pa.int64()),
        ("source_record_offset", pa.int64()),
        ("source_record_length", pa.int64()),
        ("source_record_sha256", pa.string()),
        ("source_record_json_exact", pa.large_string()),
        ("source_fields_json_canonical", pa.large_string()),
        ("source_field_names", pa.list_(pa.string())),
        ("wikidisputes_id_exact", pa.string()),
        ("wikidisputes_original_id_exact", pa.string()),
        ("wikidisputes_conv_id_exact", pa.string()),
        ("wikidisputes_reply_to_exact", pa.string()),
        ("wikidisputes_user_exact", pa.string()),
        ("wikidisputes_time", pa.string()),
        ("wikidisputes_type_exact", pa.string()),
        ("wikidisputes_text_exact", pa.large_string()),
        ("wikidisputes_pagetitle_exact", pa.string()),
        ("source_projection_sha256", pa.string()),
        ("projection_serialization_version", pa.string()),
        ("schema_version", pa.string()),
    ]
)


def source_archive_path(data_root: Path, pin: SourcePin) -> Path:
    category = "reference" if not pin.authoritative else "bronze"
    return data_root / category / "wikidisputes" / pin.commit / pin.archive


def download_pin(data_root: Path, pin: SourcePin, user_agent: str) -> dict[str, Any]:
    """Download one exact Git object with safe resume and fatal hash checking."""
    target = source_archive_path(data_root, pin)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return verify_pin(target, pin)
    partial = target.with_suffix(target.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    url = (
        "https://raw.githubusercontent.com/christinedekock11/wikidisputes/"
        f"{pin.commit}/{pin.archive}"
    )
    headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    with (
        httpx.Client(timeout=300, follow_redirects=True, headers=headers) as client,
        client.stream("GET", url) as response,
    ):
        if offset and response.status_code != 206:
            # A server that ignores Range cannot be appended safely. Keep the
            # old partial as evidence and start a separate fresh transfer.
            quarantine = (
                data_root
                / "bronze"
                / "quarantine"
                / f"{pin.commit}-{pin.archive}.range-not-honored.part"
            )
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            os.replace(partial, quarantine)
            return download_pin(data_root, pin, user_agent)
        response.raise_for_status()
        mode = "ab" if offset else "wb"
        with partial.open(mode) as output:
            for chunk in response.iter_bytes(8 * 1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    observed = sha256_file(partial)
    if observed != pin.sha256:
        quarantine = data_root / "bronze" / "quarantine" / f"{observed}-{pin.archive}"
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, quarantine)
        raise RuntimeError(
            f"fatal downloaded pin mismatch: expected {pin.sha256}, observed {observed}; "
            f"retained at {quarantine}"
        )
    os.replace(partial, target)
    return {**verify_pin(target, pin), "retrieval_url": url}


def verify_pin(path: Path, pin: SourcePin) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    observed = sha256_file(path)
    result = {
        **asdict(pin),
        "path": str(path),
        "bytes": path.stat().st_size,
        "observed_sha256": observed,
    }
    if observed != pin.sha256:
        evidence = path.with_suffix(path.suffix + ".HASH_MISMATCH.json")
        atomic_write_json(evidence, result)
        raise RuntimeError(
            f"fatal pin mismatch for {path}: expected {pin.sha256}, observed {observed}"
        )
    return result


def _safe_member(member: tarfile.TarInfo) -> bool:
    path = PurePosixPath(member.name)
    return not path.is_absolute() and ".." not in path.parts and (member.isfile() or member.isdir())


def extract_archive(data_root: Path, pin: SourcePin) -> dict[str, Any]:
    archive = source_archive_path(data_root, pin)
    archive_manifest = verify_pin(archive, pin)
    target_root = data_root / "bronze" / "extracted" / pin.sha256
    target_root.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if not all(_safe_member(member) for member in members):
            raise RuntimeError(f"unsafe archive member in {archive}")
        for member in members:
            if not member.isfile():
                continue
            target = target_root / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = source.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"failed to open archive member {member.name}")
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    shutil.copyfileobj(extracted, output, 8 * 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            except BaseException:
                with suppress(FileNotFoundError):
                    os.unlink(temporary)
                raise
            descriptor_payload = file_descriptor(target)
            descriptor_payload.update(
                {
                    "archive_member_path": member.name,
                    "tar_size": member.size,
                    "encoding": "utf-8",
                    "decoding_status": "verified" if _is_utf8(target) else "invalid_utf8",
                }
            )
            files.append(descriptor_payload)
    manifest = {"source": archive_manifest, "target_root": str(target_root), "files": files}
    atomic_write_json(target_root / "extraction_manifest.json", manifest)
    return manifest


def _is_utf8(path: Path) -> bool:
    decoder_pending = b""
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            try:
                (decoder_pending + chunk).decode("utf-8")
                decoder_pending = b""
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data" and exc.end == len(
                    decoder_pending + chunk
                ):
                    combined = decoder_pending + chunk
                    decoder_pending = combined[exc.start :]
                else:
                    return False
    if decoder_pending:
        try:
            decoder_pending.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _row_uid(pin: SourcePin, member: str, side: str, case_index: int, row_index: int) -> str:
    locator = {
        "repository": pin.repository,
        "commit": pin.commit,
        "archive": pin.archive,
        "archive_sha256": pin.sha256,
        "member": member,
        "side": side,
        "case_index": case_index,
        "row_index": row_index,
        "identity_version": "source-row-location-v1",
    }
    return "wdrow:v1:" + canonical_json_hash(locator)


def _case_uid(pin: SourcePin, member: str, side: str, case_index: int) -> str:
    return "wdcase:v1:" + canonical_json_hash([pin.commit, pin.sha256, member, side, case_index])


def iter_source_rows(
    path: Path, pin: SourcePin, member: str, side: str
) -> Iterator[dict[str, Any]]:
    file_sha = sha256_file(path)
    source_file_uid = "wdfile:v1:" + canonical_json_hash([pin.commit, pin.sha256, member, file_sha])
    source_order = 0
    with (
        path.open("rb") as handle,
        mmap.mmap(handle.fileno(), length=0, access=mmap.ACCESS_READ) as data,
    ):
        for case_index, case_span in enumerate(array_items(data, top_level_array_span(data))):
            case_bytes = bytes(data[case_span.start : case_span.end])
            case = json.loads(case_bytes)
            if not isinstance(case, dict) or not isinstance(case.get("conversation"), list):
                raise ValueError(f"invalid source case at index {case_index} in {path}")
            dispute = case.get("dispute")
            dispute_dict = dispute if isinstance(dispute, dict) else {}
            dispute_id = _text_or_none(dispute_dict.get("id"))
            conversation_span = member_span(data, case_span, b"conversation")
            for row_index, row_span in enumerate(array_items(data, conversation_span)):
                record_bytes = bytes(data[row_span.start : row_span.end])
                record = json.loads(record_bytes)
                if not isinstance(record, dict):
                    raise ValueError(f"non-object source row {case_index}/{row_index} in {path}")
                source_record_sha = sha256_bytes(record_bytes)
                row: dict[str, Any] = {
                    "source_repository": pin.repository,
                    "source_commit": pin.commit,
                    "archive_name": pin.archive,
                    "archive_sha256": pin.sha256,
                    "archive_member_path": member,
                    "source_file_sha256": file_sha,
                    "source_side": side,
                    "source_case_index": case_index,
                    "source_row_index": row_index,
                    "source_record_sha256": source_record_sha,
                    "source_record_json_exact": record_bytes.decode("utf-8"),
                }
                projection_sha = projection_hash(row, PROJECTION_FIELDS)
                yield {
                    "source_row_uid": _row_uid(pin, member, side, case_index, row_index),
                    "source_file_uid": source_file_uid,
                    **row,
                    "source_wikidisputes_escalated": side == "escalated",
                    "source_order": source_order,
                    "source_case_uid": _case_uid(pin, member, side, case_index),
                    "source_dispute_id_exact": dispute_id,
                    "source_dispute_json_canonical": canonical_json_bytes(dispute).decode("utf-8"),
                    "source_case_offset": case_span.start,
                    "source_case_length": case_span.length,
                    "source_record_offset": row_span.start,
                    "source_record_length": row_span.length,
                    "source_fields_json_canonical": canonical_json_bytes(record).decode("utf-8"),
                    "source_field_names": list(record.keys()),
                    "wikidisputes_id_exact": _text_or_none(record.get("id")),
                    "wikidisputes_original_id_exact": _text_or_none(record.get("original_id")),
                    "wikidisputes_conv_id_exact": _text_or_none(record.get("conv_id")),
                    "wikidisputes_reply_to_exact": _text_or_none(record.get("reply_to")),
                    "wikidisputes_user_exact": _text_or_none(record.get("user")),
                    "wikidisputes_time": _text_or_none(record.get("time")),
                    "wikidisputes_type_exact": _text_or_none(record.get("type")),
                    "wikidisputes_text_exact": _text_or_none(record.get("text")),
                    "wikidisputes_pagetitle_exact": _text_or_none(record.get("pagetitle")),
                    "source_projection_sha256": projection_sha,
                    "projection_serialization_version": PROJECTION_SERIALIZATION_VERSION,
                    "schema_version": SCHEMA_VERSION,
                }
                source_order += 1


def build_source_projection(
    data_root: Path,
    output_root: Path,
    *,
    pin: SourcePin = CURRENT,
    case_limit: int | None = None,
) -> dict[str, Any]:
    extracted = data_root / "bronze" / "extracted" / pin.sha256
    target = output_root / "canonical" / "wikidisputes_source_projection.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    counts: Counter[str] = Counter()
    types: Counter[str] = Counter()
    field_names: set[str] = set()
    try:
        with pq.ParquetWriter(
            temporary, SOURCE_SCHEMA, compression="zstd", compression_level=9
        ) as writer:
            for side, filename in (
                ("escalated", "escalated.json"),
                ("non_escalated", "not_escalated.json"),
            ):
                member = f"data/{filename}"
                batch: list[Mapping[str, Any]] = []
                for row in iter_source_rows(extracted / member, pin, member, side):
                    if case_limit is not None and int(row["source_case_index"]) >= case_limit:
                        break
                    batch.append(row)
                    counts[side] += 1
                    types[str(row["wikidisputes_type_exact"])] += 1
                    field_names.update(row["source_field_names"])
                    if len(batch) >= 2000:
                        writer.write_table(pa.Table.from_pylist(batch, schema=SOURCE_SCHEMA))
                        batch.clear()
                if batch:
                    writer.write_table(pa.Table.from_pylist(batch, schema=SOURCE_SCHEMA))
        os.replace(temporary, target)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise
    silver_target = output_root / "silver" / "source_rows.parquet"
    silver_target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, silver_temporary_name = tempfile.mkstemp(
        prefix=f".{silver_target.name}.", dir=silver_target.parent
    )
    os.close(descriptor)
    silver_temporary = Path(silver_temporary_name)
    try:
        silver_temporary.unlink()
        try:
            os.link(target, silver_temporary)
        except OSError:
            shutil.copyfile(target, silver_temporary)
        os.replace(silver_temporary, silver_target)
    except BaseException:
        with suppress(FileNotFoundError):
            silver_temporary.unlink()
        raise
    result = {
        **file_descriptor(target),
        "rows": {**counts, "total": sum(counts.values())},
        "types": dict(types),
        "source_field_names": sorted(field_names),
        "pilot": case_limit is not None,
        "case_limit": case_limit,
        "silver_source_rows": file_descriptor(silver_target),
    }
    if case_limit is None:
        expected_rows = EXPECTED_COUNTS["rows"]
        expected_types = EXPECTED_COUNTS["types"]
        if result["rows"] != expected_rows or result["types"] != expected_types:
            evidence = output_root / "reports" / "source_projection_count_failure.json"
            atomic_write_json(evidence, {"observed": result, "expected": EXPECTED_COUNTS})
            raise RuntimeError(f"fatal source count mismatch; see {evidence}")
    atomic_write_json(output_root / "manifests" / "source_projection.json", result)
    return result
