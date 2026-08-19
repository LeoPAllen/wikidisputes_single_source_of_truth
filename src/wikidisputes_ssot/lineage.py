from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pyarrow as pa

from .constants import CURRENT, HISTORICAL, SAMPLED, SCHEMA_VERSION, SourcePin
from .hashing import canonical_json_hash
from .io import atomic_parquet, atomic_write_json, file_descriptor
from .source import source_archive_path


def _manifest_uid(pin: SourcePin) -> str:
    return "wdmanifest:v1:" + canonical_json_hash(
        [pin.repository, pin.commit, pin.archive, pin.sha256]
    )


def materialize_source_lineage(data_root: Path, output_root: Path) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for pin in (CURRENT, HISTORICAL, SAMPLED):
        archive = source_archive_path(data_root, pin)
        manifests.append(
            {
                "source_manifest_uid": _manifest_uid(pin),
                "source_name": pin.name,
                "source_repository": pin.repository,
                "source_commit": pin.commit,
                "archive_name": pin.archive,
                "archive_sha256": pin.sha256,
                "archive_bytes": archive.stat().st_size if archive.exists() else None,
                "retrieved_at_utc": dt.datetime.fromtimestamp(
                    archive.stat().st_mtime, tz=dt.UTC
                ).isoformat()
                if archive.exists()
                else None,
                "retrieval_time_status": "local_file_mtime_provenance",
                "archive_path": str(archive),
                "authoritative": pin.authoritative,
                "retrieval_status": "verified_local" if archive.exists() else "unavailable",
                "schema_version": SCHEMA_VERSION,
            }
        )
        extraction_manifest = (
            data_root / "bronze" / "extracted" / pin.sha256 / ("extraction_manifest.json")
        )
        if not extraction_manifest.exists():
            continue
        payload = json.loads(extraction_manifest.read_text(encoding="utf-8"))
        for item in payload["files"]:
            member = str(item["archive_member_path"])
            file_sha = str(item["sha256"])
            files.append(
                {
                    "source_file_uid": "wdfile:v1:"
                    + canonical_json_hash([pin.commit, pin.sha256, member, file_sha]),
                    "source_manifest_uid": _manifest_uid(pin),
                    "source_repository": pin.repository,
                    "source_commit": pin.commit,
                    "archive_sha256": pin.sha256,
                    "archive_member_path": member,
                    "file_sha256": file_sha,
                    "byte_length": item["bytes"],
                    "encoding": item["encoding"],
                    "decoding_status": item["decoding_status"],
                    "extracted_path": item["path"],
                    "schema_version": SCHEMA_VERSION,
                }
            )
    silver = output_root / "silver"
    artifacts: dict[str, Any] = {}
    for name, rows in (("source_manifests", manifests), ("source_files", files)):
        path = silver / f"{name}.parquet"
        atomic_parquet(path, pa.Table.from_pylist(rows))
        artifacts[name] = {**file_descriptor(path), "rows": len(rows)}
    report = {
        "pins": [asdict(pin) for pin in (CURRENT, HISTORICAL, SAMPLED)],
        "artifacts": artifacts,
    }
    atomic_write_json(output_root / "reports" / "source_lineage.json", report)
    return report
