from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from .hashing import canonical_json_hash
from .io import atomic_write_json, file_descriptor


def materialize_literature_registry(registry_path: Path, output_root: Path) -> dict[str, Any]:
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    rows = []
    for publication in raw["publications"]:
        row = dict(publication)
        row["cleaning_operations_json"] = json.dumps(
            row.pop("cleaning_operations"), ensure_ascii=False, separators=(",", ":")
        )
        row["registry_entry_sha256"] = canonical_json_hash(publication)
        rows.append(row)
    output = output_root / "silver" / "literature_cleaning_registry.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), output, compression="zstd", compression_level=9)
    report = {
        "registry_version": raw["version"],
        "publication_count": len(rows),
        "search_log": raw["search_log"],
        "artifact": {**file_descriptor(output), "rows": len(rows)},
        "coverage_status": (
            "best_effort_public_full_text_search_not_exhaustive_subscription_indexes"
        ),
    }
    atomic_write_json(output_root / "reports" / "literature_registry.json", report)
    return report
