from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .constants import HISTORICAL, SCHEMA_VERSION
from .hashing import canonical_json_bytes, canonical_json_hash, sha256_bytes, sha256_file
from .io import atomic_write_json, file_descriptor

ARTICLE_SCHEMA = pa.schema(
    [
        ("article_revision_uid", pa.string()),
        ("source_repository", pa.string()),
        ("source_commit", pa.string()),
        ("archive_sha256", pa.string()),
        ("archive_member_path", pa.string()),
        ("source_file_sha256", pa.string()),
        ("source_side", pa.string()),
        ("source_case_index", pa.int64()),
        ("source_edit_index", pa.int64()),
        ("source_record_offset", pa.int64()),
        ("source_record_length", pa.int64()),
        ("source_record_sha256", pa.string()),
        ("source_record_json_exact", pa.large_string()),
        ("source_fields_json_canonical", pa.large_string()),
        ("source_field_names", pa.list_(pa.string())),
        ("conversation_id_exact", pa.string()),
        ("page_id", pa.string()),
        ("title_at_event_exact", pa.string()),
        ("revision_id", pa.string()),
        ("timestamp_exact", pa.string()),
        ("actor_name_exact", pa.string()),
        ("actor_value_json_canonical", pa.string()),
        ("edit_summary_exact", pa.large_string()),
        ("revision_sha1", pa.string()),
        ("availability_status", pa.string()),
        ("schema_version", pa.string()),
    ]
)
EVENT_SCHEMA = pa.schema(
    [
        ("event_uid", pa.string()),
        ("event_type", pa.string()),
        ("event_subtype", pa.string()),
        ("conversation_id_exact", pa.string()),
        ("event_time_exact", pa.string()),
        ("event_time_utc", pa.string()),
        ("event_time_status", pa.string()),
        ("page_id", pa.string()),
        ("title_at_event_exact", pa.string()),
        ("revision_id", pa.string()),
        ("extraction_method", pa.string()),
        ("availability_status", pa.string()),
        ("leakage_class", pa.string()),
        ("schema_version", pa.string()),
    ]
)
EVIDENCE_SCHEMA = pa.schema(
    [
        ("event_uid", pa.string()),
        ("evidence_uid", pa.string()),
        ("evidence_kind", pa.string()),
        ("source_entity_uid", pa.string()),
        ("evidence_pointer", pa.string()),
        ("evidence_sha256", pa.string()),
    ]
)


def _historical_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value, "%d/%m/%y %H:%M ").replace(tzinfo=dt.UTC).isoformat()
    except ValueError:
        return None


def _skip_ws(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t\r\n":
        position += 1
    return position


def _exact_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _iter_cases(text: str) -> Any:
    decoder = json.JSONDecoder()
    position = _skip_ws(text, 0)
    if text[position] != "[":
        raise ValueError("historical source must be a top-level JSON array")
    position += 1
    case_index = 0
    while True:
        position = _skip_ws(text, position)
        if text[position] == "]":
            return
        start = position
        case, end = decoder.raw_decode(text, position)
        yield case_index, start, end, case
        case_index += 1
        position = _skip_ws(text, end)
        if text[position] == ",":
            position += 1
        elif text[position] != "]":
            raise ValueError(f"invalid case separator at character {position}")


def _iter_exact_edits(text: str, case_start: int, case_end: int) -> Any:
    """Yield edit index, exact character span, and parsed edit.

    Source files are verified ASCII, so character offsets equal UTF-8 byte offsets.
    `rfind` locates the object key, not escaped text inside a JSON string.
    """
    boundary = '], "edits": ['
    key = text.rfind(boundary, case_start, case_end)
    if key < 0:
        return
    array_start = key + len('], "edits": ')
    if array_start < 0:
        raise ValueError(f"edits key without array near character {key}")
    decoder = json.JSONDecoder()
    position = array_start + 1
    edit_index = 0
    while True:
        position = _skip_ws(text, position)
        if text[position] == "]":
            return
        start = position
        edit, end = decoder.raw_decode(text, position)
        yield edit_index, start, end, edit
        edit_index += 1
        position = _skip_ws(text, end)
        if text[position] == ",":
            position += 1
        elif text[position] != "]":
            raise ValueError(f"invalid edit separator at character {position}")


def _temporary_target(path: Path) -> tuple[Path, pq.ParquetWriter]:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    schema = {
        "article_revisions.parquet": ARTICLE_SCHEMA,
        "events_historical_article_edits.parquet": EVENT_SCHEMA,
        "event_evidence_historical_article_edits.parquet": EVIDENCE_SCHEMA,
    }[path.name]
    return temporary, pq.ParquetWriter(temporary, schema, compression="zstd", compression_level=9)


def extract_historical_article_edits(data_root: Path, output_root: Path) -> dict[str, Any]:
    extracted = data_root / "bronze" / "extracted" / HISTORICAL.sha256
    targets = {
        "article_revisions": output_root / "silver" / "article_revisions.parquet",
        "events": output_root / "silver" / "events_historical_article_edits.parquet",
        "event_evidence": output_root
        / "silver"
        / "event_evidence_historical_article_edits.parquet",
    }
    targets["article_revisions"].parent.mkdir(parents=True, exist_ok=True)
    temporary_writers: dict[str, tuple[Path, pq.ParquetWriter]] = {
        name: _temporary_target(path) for name, path in targets.items()
    }
    batches: dict[str, list[dict[str, Any]]] = {name: [] for name in targets}
    counts: Counter[str] = Counter()
    field_names: set[str] = set()

    def flush() -> None:
        schemas = {
            "article_revisions": ARTICLE_SCHEMA,
            "events": EVENT_SCHEMA,
            "event_evidence": EVIDENCE_SCHEMA,
        }
        for name, batch in batches.items():
            if batch:
                temporary_writers[name][1].write_table(
                    pa.Table.from_pylist(batch, schema=schemas[name])
                )
                batch.clear()

    try:
        for side, filename in (
            ("escalated", "escalated.json"),
            ("non_escalated", "not_escalated.json"),
        ):
            member = f"data/{filename}"
            path = extracted / member
            raw_file = path.read_bytes()
            if not raw_file.isascii():
                raise RuntimeError(
                    f"{path} is not ASCII; character offsets cannot serve as byte offsets"
                )
            text = raw_file.decode("ascii")
            file_sha = sha256_file(path)
            for case_index, case_start, case_end, case in _iter_cases(text):
                dispute = case.get("dispute") if isinstance(case, dict) else None
                found_edit = False
                for edit_index, edit_start, edit_end, edit in _iter_exact_edits(
                    text, case_start, case_end
                ):
                    found_edit = True
                    if not isinstance(edit, dict):
                        counts["non_object_edits"] += 1
                        continue
                    raw = raw_file[edit_start:edit_end]
                    field_names.update(edit)
                    conv_id = _exact_string(edit.get("conv_id"))
                    revision_id = edit.get("id")
                    article_revision_uid = "wdarticle-revision:v1:" + canonical_json_hash(
                        [HISTORICAL.commit, HISTORICAL.sha256, member, case_index, edit_index]
                    )
                    event_uid = "wdevent:v1:" + canonical_json_hash(
                        [article_revision_uid, "article_edit"]
                    )
                    evidence_uid = "wdevidence:v1:" + canonical_json_hash(
                        [article_revision_uid, sha256_bytes(raw)]
                    )
                    user = edit.get("user", edit.get("username"))
                    title = _exact_string(edit.get("pagetitle")) or _exact_string(
                        dispute.get("pagetitle") if isinstance(dispute, dict) else None
                    )
                    if title is None and isinstance(dispute, dict):
                        pages = dispute.get("pages")
                        if isinstance(pages, list) and len(pages) == 1:
                            title = _exact_string(pages[0])
                    row = {
                        "article_revision_uid": article_revision_uid,
                        "source_repository": HISTORICAL.repository,
                        "source_commit": HISTORICAL.commit,
                        "archive_sha256": HISTORICAL.sha256,
                        "archive_member_path": member,
                        "source_file_sha256": file_sha,
                        "source_side": side,
                        "source_case_index": case_index,
                        "source_edit_index": edit_index,
                        "source_record_offset": edit_start,
                        "source_record_length": edit_end - edit_start,
                        "source_record_sha256": sha256_bytes(raw),
                        "source_record_json_exact": raw.decode("ascii"),
                        "source_fields_json_canonical": canonical_json_bytes(edit).decode("utf-8"),
                        "source_field_names": list(edit.keys()),
                        "conversation_id_exact": conv_id,
                        "page_id": None,
                        "title_at_event_exact": title,
                        "revision_id": str(revision_id) if revision_id is not None else None,
                        "timestamp_exact": _exact_string(edit.get("time", edit.get("timestamp"))),
                        "actor_name_exact": user if isinstance(user, str) else None,
                        "actor_value_json_canonical": canonical_json_bytes(user).decode("utf-8"),
                        "edit_summary_exact": _exact_string(edit.get("comment")),
                        "revision_sha1": None,
                        "availability_status": "historical_release_metadata_only",
                        "schema_version": SCHEMA_VERSION,
                    }
                    batches["article_revisions"].append(row)
                    batches["events"].append(
                        {
                            "event_uid": event_uid,
                            "event_type": "article_edit",
                            "event_subtype": "historical_wikidisputes_edit_summary",
                            "conversation_id_exact": conv_id,
                            "event_time_exact": row["timestamp_exact"],
                            "event_time_utc": _historical_time(row["timestamp_exact"]),
                            "event_time_status": "parsed"
                            if _historical_time(row["timestamp_exact"])
                            else "source_exact_unparsed",
                            "page_id": None,
                            "title_at_event_exact": title,
                            "revision_id": row["revision_id"],
                            "extraction_method": "historical_pre_removal_release_edits_array",
                            "availability_status": "available_metadata_content_not_hydrated",
                            "leakage_class": "raw_event_unclassified_against_episode_index",
                            "schema_version": SCHEMA_VERSION,
                        }
                    )
                    batches["event_evidence"].append(
                        {
                            "event_uid": event_uid,
                            "evidence_uid": evidence_uid,
                            "evidence_kind": "historical_source_record_bytes",
                            "source_entity_uid": article_revision_uid,
                            "evidence_pointer": f"{member}#bytes={edit_start}-{edit_end}",
                            "evidence_sha256": row["source_record_sha256"],
                        }
                    )
                    counts[side] += 1
                    if len(batches["article_revisions"]) >= 10_000:
                        flush()
                if not found_edit:
                    counts["cases_with_empty_or_missing_edits"] += 1
            del text
            del raw_file
        flush()
        for _, writer in temporary_writers.values():
            writer.close()
        for name, target in targets.items():
            os.replace(temporary_writers[name][0], target)
    except BaseException:
        for temporary, writer in temporary_writers.values():
            with suppress(Exception):
                writer.close()
            with suppress(FileNotFoundError):
                temporary.unlink()
        raise

    descriptors = {
        name: {**file_descriptor(path), "rows": counts["total"]} for name, path in targets.items()
    }
    counts["total"] = counts["escalated"] + counts["non_escalated"]
    for descriptor in descriptors.values():
        descriptor["rows"] = counts["total"]
    report = {
        "status": "complete_for_pinned_historical_edits_arrays",
        "counts": dict(counts),
        "source_field_names": sorted(field_names),
        "artifacts": descriptors,
        "limitation": (
            "historical release supplies edit metadata/summaries, not revision content or SHA-1; "
            "those remain targeted hydration work"
        ),
    }
    atomic_write_json(output_root / "reports" / "historical_article_edits.json", report)
    return report
