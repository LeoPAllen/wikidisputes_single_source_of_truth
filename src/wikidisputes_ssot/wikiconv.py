from __future__ import annotations

import json
import mmap
import os
import random
import shutil
import tempfile
import time
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from .config import Settings
from .exact_json import Span, object_members, skip_ws, value_end
from .hashing import canonical_json_bytes, canonical_json_hash, sha256_bytes, sha256_file
from .io import atomic_write_json, file_descriptor


def top_level_object_span(data: mmap.mmap) -> Span:
    start = skip_ws(data, 0)
    end = value_end(data, start)
    if data[start] != 0x7B or skip_ws(data, end) != len(data):
        raise ValueError("conversation metadata must be exactly one top-level JSON object")
    return Span(start, end)


def _archive_inventory(path: Path) -> dict[int, dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {int(year): dict(value) for year, value in raw["archives"].items()}


def _url(settings: Settings, year: int) -> str:
    return f"{settings.wikiconv.base_url}/wikiconv-{year}/full.corpus.zip"


def _validate_zip(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise RuntimeError(f"corrupt WikiConv ZIP member: {corrupt}")
        required = {"utterances.jsonl", "conversations.json", "speakers.json"}
        names = set(archive.namelist())
        missing = required - names
        if missing:
            raise RuntimeError(f"WikiConv ZIP missing members: {sorted(missing)}")
        return [
            {
                "name": info.filename,
                "uncompressed_bytes": info.file_size,
                "compressed_bytes": info.compress_size,
                "crc32": f"{info.CRC:08x}",
            }
            for info in archive.infolist()
        ]


def download_year(
    settings: Settings,
    year: int,
    inventory_path: Path,
    *,
    staging_root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Resume one annual archive with strict byte-range validation."""
    inventory = _archive_inventory(inventory_path)
    if year not in inventory:
        raise ValueError(f"year {year} is not in pinned WikiConv inventory")
    expected = inventory[year]
    root = staging_root or settings.roots.data / "staging" / "wikiconv"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"wikiconv-english-{year}.zip"
    part = target.with_suffix(".zip.part")
    if target.exists() and target.stat().st_size == expected["bytes"]:
        observed_sha = sha256_file(target)
        if expected.get("sha256") and observed_sha != expected["sha256"]:
            raise RuntimeError(f"existing WikiConv {year} archive hash mismatch")
        return target, _download_manifest(settings, year, target, observed_sha, "cached")
    if target.exists():
        target.replace(part)
    offset = part.stat().st_size if part.exists() else 0
    if offset > int(expected["bytes"]):
        raise RuntimeError(f"partial WikiConv {year} archive is larger than expected")
    headers = {
        "User-Agent": settings.network.user_agent,
        "Accept-Encoding": "identity",
    }
    if offset:
        headers["Range"] = f"bytes={offset}-"
    url = _url(settings, year)
    attempts = settings.network.max_attempts
    for attempt in range(1, attempts + 1):
        try:
            timeout = httpx.Timeout(settings.network.timeout_seconds)
            with (
                httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client,
                client.stream("GET", url) as response,
            ):
                if offset:
                    expected_range = f"bytes {offset}-"
                    observed_range = response.headers.get("content-range", "")
                    if response.status_code != 206 or not observed_range.startswith(expected_range):
                        raise RuntimeError(
                            "server did not honor the exact WikiConv resume range; "
                            f"status={response.status_code} content-range={observed_range!r}"
                        )
                elif response.status_code != 200:
                    response.raise_for_status()
                retry_after = response.headers.get("retry-after")
                if response.status_code in {429, 503}:
                    raise httpx.HTTPStatusError(
                        f"rate limited; retry-after={retry_after}",
                        request=response.request,
                        response=response,
                    )
                mode = "ab" if offset else "wb"
                with part.open(mode) as output:
                    for chunk in response.iter_bytes(8 * 1024 * 1024):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if part.stat().st_size != int(expected["bytes"]):
                raise RuntimeError(
                    f"WikiConv {year} size mismatch: expected {expected['bytes']}, "
                    f"observed {part.stat().st_size}"
                )
            observed_sha = sha256_file(part)
            if expected.get("sha256") and observed_sha != expected["sha256"]:
                raise RuntimeError(
                    f"WikiConv {year} hash mismatch: expected {expected['sha256']}, "
                    f"observed {observed_sha}"
                )
            os.replace(part, target)
            return target, _download_manifest(settings, year, target, observed_sha, "downloaded")
        except (httpx.HTTPError, OSError) as exc:
            if attempt == attempts:
                raise RuntimeError(
                    f"WikiConv {year} download failed after {attempts} attempts"
                ) from exc
            delay = min(60.0, 2.0**attempt) + random.Random(year * 100 + attempt).random()
            time.sleep(delay)
            offset = part.stat().st_size if part.exists() else 0
            headers["Range"] = f"bytes={offset}-" if offset else ""
    raise AssertionError("unreachable")


def _download_manifest(
    settings: Settings, year: int, path: Path, archive_sha256: str, status: str
) -> dict[str, Any]:
    return {
        "year": year,
        "language": settings.wikiconv.language,
        "url": _url(settings, year),
        "archive_sha256": archive_sha256,
        "archive_bytes": path.stat().st_size,
        "retrieval_status": status,
        "zip_members": _validate_zip(path),
    }


def _selected_ids(path: Path) -> set[str]:
    table = pq.read_table(path, columns=["conversation_id_exact"])
    return {str(value) for value in table.column(0).to_pylist() if value is not None}


def _extract_selected_conversation_metadata(
    archive: zipfile.ZipFile,
    selected: set[str],
    temporary_root: Path,
    output_path: Path,
) -> tuple[int, str]:
    temporary_root.mkdir(parents=True, exist_ok=True)
    extracted = temporary_root / "conversations.json"
    with archive.open("conversations.json") as source, extracted.open("wb") as output:
        shutil.copyfileobj(source, output, 8 * 1024 * 1024)
    count = 0
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", dir=output_path.parent
    )
    try:
        with (
            os.fdopen(descriptor, "wb") as output,
            extracted.open("rb") as handle,
            mmap.mmap(handle.fileno(), length=0, access=mmap.ACCESS_READ) as data,
        ):
            for raw_key, span in object_members(data, top_level_object_span(data)):
                key = json.loads(b'"' + raw_key + b'"')
                if key not in selected:
                    continue
                record = {
                    "conversation_id": key,
                    "metadata_json_exact": bytes(data[span.start : span.end]).decode("utf-8"),
                    "metadata_byte_offset_in_member": span.start,
                    "metadata_byte_length": span.length,
                }
                output.write(canonical_json_bytes(record) + b"\n")
                count += 1
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, output_path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
    finally:
        with suppress(FileNotFoundError):
            extracted.unlink()
    return count, sha256_file(output_path)


def filter_year(
    settings: Settings,
    year: int,
    archive_path: Path,
    archive_manifest: dict[str, Any],
    selected_conversations_path: Path,
) -> dict[str, Any]:
    selected = _selected_ids(selected_conversations_path)
    bronze = settings.roots.data / "bronze" / "wikiconv" / "selected" / str(year)
    bronze.mkdir(parents=True, exist_ok=True)
    exact_path = bronze / "utterances.jsonl"
    metadata_path = bronze / "conversations.selected.jsonl"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".utterances.", dir=bronze)
    selected_count = 0
    seen_conversations: set[str] = set()
    normalized: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(archive_path) as archive, os.fdopen(descriptor, "wb") as exact:
            with archive.open("utterances.jsonl") as source:
                for line_index, raw_line in enumerate(source):
                    record = json.loads(raw_line)
                    conversation_id = record.get("conversation_id")
                    if conversation_id not in selected:
                        continue
                    exact.write(raw_line)
                    selected_count += 1
                    seen_conversations.add(conversation_id)
                    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
                    raw_sha = sha256_bytes(raw_line.rstrip(b"\r\n"))
                    normalized.append(
                        {
                            "wikiconv_source_row_uid": "wcrow:v1:"
                            + canonical_json_hash(
                                [archive_manifest["archive_sha256"], "utterances.jsonl", line_index]
                            ),
                            "corpus_year": year,
                            "archive_sha256": archive_manifest["archive_sha256"],
                            "archive_bytes": archive_manifest["archive_bytes"],
                            "archive_url": archive_manifest["url"],
                            "archive_member": "utterances.jsonl",
                            "source_line_index": line_index,
                            "source_record_sha256": raw_sha,
                            "source_record_json_exact": raw_line.rstrip(b"\r\n").decode("utf-8"),
                            "wikiconv_id_exact": record.get("id"),
                            "conversation_id_exact": conversation_id,
                            "wikiconv_text_exact": record.get("text"),
                            "wikiconv_speaker_exact": record.get("speaker")
                            if isinstance(record.get("speaker"), str)
                            else canonical_json_bytes(record.get("speaker")).decode("utf-8"),
                            "wikiconv_reply_to_exact": record.get("reply-to"),
                            "wikiconv_timestamp_unix": record.get("timestamp"),
                            "is_section_header": meta.get("is_section_header"),
                            "indentation_exact": meta.get("indentation"),
                            "ancestor_id_exact": meta.get("ancestor_id"),
                            "revision_id_exact": str(meta.get("rev_id"))
                            if meta.get("rev_id") is not None
                            else None,
                            "parent_id_exact": meta.get("parent_id"),
                            "meta_json_canonical": canonical_json_bytes(meta).decode("utf-8"),
                            "recovery_status": "recovered_from_pinned_annual_corpus",
                        }
                    )
            exact.flush()
            os.fsync(exact.fileno())
            os.replace(temporary_name, exact_path)
            metadata_count, metadata_sha = _extract_selected_conversation_metadata(
                archive,
                selected,
                settings.roots.data / "staging" / "wikiconv" / f"metadata-{year}",
                metadata_path,
            )
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
    parquet_path = bronze / "utterances.parquet"
    table = (
        pa.Table.from_pylist(normalized)
        if normalized
        else pa.table({"corpus_year": [year]}).slice(0, 0)
    )
    pq.write_table(table, parquet_path, compression="zstd", compression_level=9)
    manifest = {
        **archive_manifest,
        "selected_conversation_population": len(selected),
        "selected_conversations_observed": len(seen_conversations),
        "selected_utterance_rows": selected_count,
        "selected_conversation_metadata_rows": metadata_count,
        "selected_utterances_jsonl": file_descriptor(exact_path),
        "selected_conversations_jsonl": {
            **file_descriptor(metadata_path),
            "sha256": metadata_sha,
        },
        "selected_utterances_parquet": file_descriptor(parquet_path),
        "full_archive_retained": False,
        "retention_reason": "rolling low-disk filter; exact selected rows and archive pin retained",
    }
    atomic_write_json(bronze / "manifest.json", manifest)
    return manifest


def enumerate_year(
    settings: Settings,
    year: int,
    inventory_path: Path,
    selected_conversations_path: Path,
    *,
    keep_archive: bool = False,
) -> dict[str, Any]:
    archive, download_manifest = download_year(settings, year, inventory_path)
    result = filter_year(
        settings,
        year,
        archive,
        download_manifest,
        selected_conversations_path,
    )
    if not keep_archive:
        archive.unlink()
    return result


def merge_enumeration(settings: Settings) -> dict[str, Any]:
    paths = [
        settings.roots.data / "bronze" / "wikiconv" / "selected" / str(year) / "utterances.parquet"
        for year in settings.wikiconv.years
    ]
    missing_years = [
        year for year, path in zip(settings.wikiconv.years, paths, strict=True) if not path.exists()
    ]
    if missing_years:
        raise RuntimeError(f"cannot merge before every annual scan completes: {missing_years}")
    tables = [pq.read_table(path) for path in paths]
    merged = pa.concat_tables(tables, promote_options="default")
    output = settings.roots.output / "silver" / "wikiconv_selected_rows.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    records = merged.to_pylist()
    identity_content: dict[str, set[str]] = {}
    identity_observations: dict[tuple[str, str], list[dict[str, Any]]] = {}
    conversation_years: dict[str, set[int]] = {}
    for row in records:
        action_identity = str(row["wikiconv_id_exact"])
        content_hash = str(row["source_record_sha256"])
        identity_content.setdefault(action_identity, set()).add(content_hash)
        identity_observations.setdefault((action_identity, content_hash), []).append(row)
        conversation_years.setdefault(str(row["conversation_id_exact"]), set()).add(
            int(row["corpus_year"])
        )
    deduplicated_records = [
        min(
            observations,
            key=lambda row: (int(row["corpus_year"]), int(row["source_line_index"])),
        )
        for observations in identity_observations.values()
    ]
    deduplicated_records.sort(
        key=lambda row: (
            int(row["corpus_year"]),
            int(row["source_line_index"]),
            str(row["wikiconv_id_exact"]),
        )
    )
    pq.write_table(
        pa.Table.from_pylist(deduplicated_records),
        output,
        compression="zstd",
        compression_level=9,
    )
    conflicting = {key: sorted(value) for key, value in identity_content.items() if len(value) > 1}
    selected_path = settings.roots.output / "silver" / "selected_conversations.parquet"
    selected_ids = _selected_ids(selected_path)
    observed_ids = set(conversation_years)

    metadata_observations: dict[tuple[str, str], dict[str, Any]] = {}
    metadata_hashes: dict[str, set[str]] = {}
    annual_sources: list[dict[str, Any]] = []
    for year in settings.wikiconv.years:
        annual_manifest_path = (
            settings.roots.data / "bronze" / "wikiconv" / "selected" / str(year) / "manifest.json"
        )
        annual_manifest = json.loads(annual_manifest_path.read_text(encoding="utf-8"))
        annual_sources.append(
            {
                key: annual_manifest[key]
                for key in (
                    "year",
                    "url",
                    "archive_sha256",
                    "archive_bytes",
                    "selected_conversations_observed",
                    "selected_utterance_rows",
                    "full_archive_retained",
                )
            }
        )
        metadata_path = (
            settings.roots.data
            / "bronze"
            / "wikiconv"
            / "selected"
            / str(year)
            / "conversations.selected.jsonl"
        )
        with metadata_path.open(encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                conversation_id = str(item["conversation_id"])
                exact = str(item["metadata_json_exact"])
                content_hash = sha256_bytes(exact.encode("utf-8"))
                metadata_hashes.setdefault(conversation_id, set()).add(content_hash)
                metadata_observations.setdefault(
                    (conversation_id, content_hash),
                    {
                        "conversation_id_exact": conversation_id,
                        "metadata_json_exact": exact,
                        "metadata_sha256": content_hash,
                        "corpus_year": year,
                        "recovery_status": "recovered_from_pinned_annual_corpus",
                    },
                )
    metadata_output = settings.roots.output / "silver" / "wikiconv_conversation_metadata.parquet"
    metadata_rows = sorted(
        metadata_observations.values(),
        key=lambda row: (row["conversation_id_exact"], row["metadata_sha256"]),
    )
    pq.write_table(
        pa.Table.from_pylist(metadata_rows),
        metadata_output,
        compression="zstd",
        compression_level=9,
    )
    selected_rows = pq.read_table(selected_path).to_pylist()
    for row in selected_rows:
        conversation_id = str(row["conversation_id_exact"])
        years = sorted(conversation_years.get(conversation_id, set()))
        row["years_scanned"] = settings.wikiconv.years
        row["years_observed"] = years
        row["recovery_status"] = (
            "enumerated_across_all_annual_corpora" if years else "missing_from_all_annual_corpora"
        )
        row["recovery_method"] = "pinned_annual_union"
    pq.write_table(
        pa.Table.from_pylist(selected_rows),
        selected_path,
        compression="zstd",
        compression_level=9,
    )
    report = {
        "years_scanned": settings.wikiconv.years,
        "selected_conversations": len(selected_ids),
        "observed_conversations": len(observed_ids),
        "missing_conversations": sorted(selected_ids - observed_ids),
        "selected_rows_before_identity_dedup": len(records),
        "selected_rows_after_identical_identity_dedup": len(deduplicated_records),
        "unique_action_identities": len(identity_content),
        "identical_cross_year_duplicate_rows_removed": len(records) - len(deduplicated_records),
        "conflicting_action_identity_count": len(conflicting),
        "conflicting_action_identities": conflicting,
        "cross_year_conversations": sum(len(years) > 1 for years in conversation_years.values()),
        "conversation_metadata_observations": len(metadata_rows),
        "conversation_metadata_conflict_count": sum(
            len(hashes) > 1 for hashes in metadata_hashes.values()
        ),
        "status": "complete"
        if selected_ids <= observed_ids and not conflicting
        else "gaps_or_conflicts",
        "output": file_descriptor(output),
        "metadata_output": file_descriptor(metadata_output),
        "annual_sources": annual_sources,
    }
    report_path = settings.roots.output / "reports" / "conversation_enumeration.json"
    atomic_write_json(report_path, report)
    return report
