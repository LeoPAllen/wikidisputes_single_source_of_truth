"""Run Method-A's monotonic additive fallback pass from frozen baseline artifacts."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from wikidisputes_ssot.io import (
    atomic_parquet,
    atomic_write_bytes,
    atomic_write_json,
    table_from_union_pylist,
)
from wikidisputes_ssot.method_a_additive import FALLBACK_TIERS, build_additive_rows

ROOT = Path.cwd()
BASELINE = ROOT / "output/reports/method_a_conservative_fallbacks_baseline"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-recovery",
        type=Path,
        default=BASELINE / "mediawiki_raw_comment_recovery.parquet",
    )
    parser.add_argument(
        "--baseline-audit",
        type=Path,
        default=BASELINE / "mediawiki_raw_comment_promotion_audit.parquet",
    )
    parser.add_argument(
        "--output-parquet",
        type=Path,
        default=ROOT / "output/silver/mediawiki_raw_comment_recovery.parquet",
    )
    parser.add_argument(
        "--output-csv", type=Path, default=ROOT / "output/silver/mediawiki_raw_comment_recovery.csv"
    )
    parser.add_argument(
        "--review-csv",
        type=Path,
        default=ROOT / "reports/mediawiki_raw_comment_recovery_review.csv",
    )
    parser.add_argument(
        "--summary", type=Path, default=ROOT / "reports/mediawiki_raw_comment_recovery_summary.json"
    )
    parser.add_argument("--expected-uid-count", type=int, default=133_223)
    return parser.parse_args()


def _load_recovery_module() -> Any:
    path = ROOT / "scripts/recover_raw_mediawiki_comments.py"
    spec = importlib.util.spec_from_file_location("method_a_recovery_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load recovery helpers: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _atomic_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_bytes(path, buffer.getvalue().encode("utf-8"))


def main() -> None:
    args = parse_args()
    recovery_rows = pq.read_table(args.baseline_recovery).to_pylist()
    audit_rows = pq.read_table(args.baseline_audit).to_pylist()
    helpers = _load_recovery_module()
    db = helpers.open_cache()

    def revision_content(revision_id: int) -> tuple[str, str | None] | None:
        revision = helpers.get_revision(db, revision_id)
        if revision is None or revision.get("status") != "found" or not revision.get("content"):
            return None
        return str(revision["content"]), revision.get("revision_user")

    try:
        rows, counts = build_additive_rows(
            recovery_rows,
            audit_rows,
            revision_content,
            helpers.candidate_comments,
            helpers.rank_candidates,
            helpers.classify,
            helpers.legacy_candidate_comments,
            legacy_source_revision=helpers.LEGACY_CANDIDATE_SOURCE_REVISION,
            expected_uid_count=args.expected_uid_count,
        )
    finally:
        db.close()
    fields = list(dict.fromkeys(key for row in rows for key in row))
    table = table_from_union_pylist(rows)
    atomic_parquet(args.output_parquet, table)
    _atomic_csv(args.output_csv, rows, fields)
    _atomic_csv(
        args.review_csv,
        [row for row in rows if row.get("recovery_status") != "high_confidence"],
        fields,
    )
    summary = {
        "status": "complete",
        "mode": "monotonic_additive_fallbacks",
        "baseline_recovery": str(args.baseline_recovery),
        "baseline_audit": str(args.baseline_audit),
        "uid_count": len(rows),
        "fallback_counts": {tier: counts.get(tier, 0) for tier in FALLBACK_TIERS},
        "baseline_promote_preserved": counts.get("baseline_promote_preserved", 0),
        "baseline_retained": counts.get("baseline_retained", 0),
        "output_parquet": str(args.output_parquet),
        "output_csv": str(args.output_csv),
        "review_csv": str(args.review_csv),
    }
    atomic_write_json(args.summary, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
