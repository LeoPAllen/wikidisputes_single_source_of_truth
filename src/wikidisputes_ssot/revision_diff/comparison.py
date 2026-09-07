"""Compare selectable Method-B evidence across two Parquet artifacts.

The baseline selectable population is the set of rows whose status is
``b_safe`` or ``b_usable``.  Every such row must remain present, retain its
status, exact candidate raw/body text, and exact archival provenance in the
new artifact.  The report is written before the command exits nonzero so a
failed comparison remains inspectable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from wikidisputes_ssot.io import atomic_write_json

SELECTABLE_STATUSES = frozenset({"b_safe", "b_usable"})
CANDIDATE_FIELDS = ("candidate_raw", "candidate_body")

# These fields identify the exact revision response/evidence provenance.  The
# candidate text and its boundaries are compared separately because a
# provenance-preserving rerun can still change candidate selection.
PROVENANCE_FIELDS = (
    "logical_utterance_uid",
    "action_uid",
    "action_type",
    "target_revision_id",
    "predecessor_revision_id",
    "page_id",
    "method",
    "method_version",
    "safety_version",
    "schema_version",
    "target_api_sha1",
    "predecessor_api_sha1",
    "target_local_content_sha256",
    "predecessor_local_content_sha256",
    "target_response_hash",
    "predecessor_response_hash",
    "target_content_pointer",
    "predecessor_content_pointer",
)


class MethodBEvidenceRegressionError(RuntimeError):
    """Raised when a baseline selectable Method-B row changes or disappears."""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _uid(row: Mapping[str, Any]) -> str:
    value = row.get("source_row_uid")
    if value in (None, ""):
        raise ValueError("row is missing source_row_uid")
    return str(value)


def _index(rows: Iterable[Mapping[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for source in rows:
        row = dict(source)
        uid = _uid(row)
        if uid in indexed:
            raise ValueError(f"duplicate source_row_uid in {label}: {uid}")
        indexed[uid] = row
    return indexed


def _exact_value(row: Mapping[str, Any], field: str) -> Any:
    """Keep missing and explicit null distinct in the comparison report."""
    return row.get(field, {"missing": True})


def _digest(value: Any) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _change(uid: str, field: str, before: Any, after: Any) -> dict[str, Any]:
    return {
        "source_row_uid": uid,
        "field": field,
        "before": before,
        "after": after,
        "before_sha256": _digest(before),
        "after_sha256": _digest(after),
    }


def compare_method_b_evidence(
    baseline: Iterable[Mapping[str, Any]],
    current: Iterable[Mapping[str, Any]],
    *,
    raise_on_regression: bool = True,
) -> dict[str, Any]:
    """Compare all baseline ``b_safe``/``b_usable`` rows by source UID."""

    old = _index(baseline, "baseline Method-B evidence")
    new = _index(current, "current Method-B evidence")
    old_selectable = {
        uid
        for uid, row in old.items()
        if _text(row.get("status")).casefold() in SELECTABLE_STATUSES
    }
    missing = sorted(old_selectable - set(new))
    extra = sorted(set(new) - set(old))

    status_changes: list[dict[str, Any]] = []
    candidate_changes: list[dict[str, Any]] = []
    provenance_mismatches: list[dict[str, Any]] = []
    for uid in sorted(old_selectable & set(new)):
        before, after = old[uid], new[uid]
        old_status = _exact_value(before, "status")
        new_status = _exact_value(after, "status")
        if old_status != new_status:
            status_changes.append(_change(uid, "status", old_status, new_status))
        for field in CANDIDATE_FIELDS:
            old_value = _exact_value(before, field)
            new_value = _exact_value(after, field)
            if old_value != new_value:
                candidate_changes.append(_change(uid, field, old_value, new_value))
        for field in PROVENANCE_FIELDS:
            old_value = _exact_value(before, field)
            new_value = _exact_value(after, field)
            if old_value != new_value:
                provenance_mismatches.append(_change(uid, field, old_value, new_value))

    regressions = {
        "lost_rows": [{"source_row_uid": uid} for uid in missing],
        "status_changes": status_changes,
        "candidate_raw_changes": [
            row for row in candidate_changes if row["field"] == "candidate_raw"
        ],
        "candidate_body_changes": [
            row for row in candidate_changes if row["field"] == "candidate_body"
        ],
        "provenance_mismatches": provenance_mismatches,
    }
    hard_count = sum(len(values) for values in regressions.values())
    report: dict[str, Any] = {
        "status": "pass" if hard_count == 0 else "fail",
        "baseline_rows": len(old),
        "current_rows": len(new),
        "baseline_selectable_rows": len(old_selectable),
        "current_extra_rows": len(extra),
        "selectable_statuses": sorted(SELECTABLE_STATUSES),
        "regressions": {
            **regressions,
            "hard_regression_count": hard_count,
            "hard_regression_uid_count": len(
                {change["source_row_uid"] for values in regressions.values() for change in values}
            ),
        },
    }
    if raise_on_regression and hard_count:
        raise MethodBEvidenceRegressionError("Method-B evidence regression(s) detected")
    return report


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = compare_method_b_evidence(
        read_parquet_rows(args.baseline),
        read_parquet_rows(args.current),
        raise_on_regression=False,
    )
    atomic_write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["regressions"]["hard_regression_count"]:
        raise MethodBEvidenceRegressionError("Method-B evidence regression(s) detected")


if __name__ == "__main__":
    main()
