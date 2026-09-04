"""Before/after accounting for the boundary and usability pilot change.

This module deliberately operates on mappings so the workflow can use archived
Parquet rows while tests (and later audit tooling) can use in-memory evidence.
It reports Method A comparisons as controls, never as adjudicated truth.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .safety import SOFT_USABILITY_REASONS

TRACKED_UID_SUFFIXES = (
    "0c78c7423168a1a3c159",
    "bbab7c895a9914061921",
    "ed24cf692063f95d7aa5",
    "d4cd0aa46ab5ed80fe4d",
    "b334660d8add861376e1",
    "bbfba59023d4b36898d2",
)
B_STATUSES = (
    "b_safe",
    "b_usable",
    "b_review",
    "b_ambiguous",
    "b_no_candidate",
    "b_unavailable",
)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _truthy(value: Any) -> bool:
    return value is True or _text(value).strip().casefold() in {"1", "true", "yes"}


def _uid(row: Mapping[str, Any]) -> str:
    for name in ("source_row_uid", "entity_uid", "revision_diff_uid", "id", "uid"):
        if row.get(name) not in (None, ""):
            return _text(row[name])
    return ""


def _status(row: Mapping[str, Any]) -> str:
    return _text(row.get("method_b_status") or row.get("status"))


def _method_a_status(row: Mapping[str, Any]) -> str:
    return _text(row.get("method_a_status")).casefold()


def _json_reasons(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return sorted({_text(item) for item in value if _text(item)})
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(_text(value))
    except json.JSONDecodeError:
        return [part.strip() for part in _text(value).split("|") if part.strip()]
    return (
        sorted({_text(item) for item in parsed if _text(item)}) if isinstance(parsed, list) else []
    )


def reason_codes(row: Mapping[str, Any]) -> list[str]:
    """Extract recovery reason codes without imposing a storage schema."""
    for name in ("reason_codes_json", "safety_reason_codes_json", "reason_codes", "reasons"):
        if name in row:
            return _json_reasons(row.get(name))
    return []


def normalize_offset(value: Any) -> int | None:
    """Normalize offsets for comparison only; source row values stay untouched."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.lstrip("+-").isdigit():
            return int(stripped)
    return None


def _boundary(row: Mapping[str, Any], side: str, method: str) -> Any:
    nested = row.get(f"{side}_boundaries")
    if isinstance(nested, Mapping):
        return nested.get(method)
    prefix = "a" if method == "method_a" else "b"
    first = row.get(f"{prefix}_{'left' if side == 'left' else 'right'}")
    return first if first is not None else row.get(f"{prefix}_{side}_boundary")


def _body_equal(row: Mapping[str, Any], visible: bool) -> bool | None:
    candidate = row.get("candidate")
    key = "visible_equal" if visible else "raw_equal"
    value = (
        candidate.get(key)
        if isinstance(candidate, Mapping)
        else row.get("visible_text_equal" if visible else "raw_body_equal")
    )
    return value if isinstance(value, bool) else None


def _metric(comparable: int, agreement: int) -> dict[str, int | float | None]:
    return {
        "comparable_count": comparable,
        "agreement_count": agreement,
        "agreement_rate": agreement / comparable if comparable else None,
    }


def comparison_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Calculate comparison denominators with normalized offset equality."""
    materialized = list(rows)
    raw_comparable = raw_agree = visible_comparable = visible_agree = 0
    left_comparable = left_agree = right_comparable = right_agree = both_comparable = both_agree = 0
    for row in materialized:
        candidate = row.get("candidate")
        comparable = (
            candidate.get("comparable")
            if isinstance(candidate, Mapping)
            else row.get("text_comparable")
        )
        if comparable is True:
            raw_comparable += 1
            visible_comparable += 1
            raw_agree += _body_equal(row, False) is True
            visible_agree += _body_equal(row, True) is True
        left = (_boundary(row, "left", "method_a"), _boundary(row, "left", "method_b"))
        right = (_boundary(row, "right", "method_a"), _boundary(row, "right", "method_b"))
        normalized_left = tuple(normalize_offset(value) for value in left)
        normalized_right = tuple(normalize_offset(value) for value in right)
        left_ok = all(value is not None for value in normalized_left)
        right_ok = all(value is not None for value in normalized_right)
        if left_ok:
            left_comparable += 1
            left_agree += normalized_left[0] == normalized_left[1]
        if right_ok:
            right_comparable += 1
            right_agree += normalized_right[0] == normalized_right[1]
        if left_ok and right_ok:
            both_comparable += 1
            both_agree += (
                normalized_left[0] == normalized_left[1]
                and normalized_right[0] == normalized_right[1]
            )
    return {
        "raw_body": _metric(raw_comparable, raw_agree),
        "visible_text": _metric(visible_comparable, visible_agree),
        "left_boundary": _metric(left_comparable, left_agree),
        "right_boundary": _metric(right_comparable, right_agree),
        "both_boundaries": _metric(both_comparable, both_agree),
    }


def _snapshot(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    materialized = [dict(row) for row in rows]
    assignment = Counter(_text(row.get("assignment_status")) or "missing" for row in materialized)
    statuses = Counter(_status(row) for row in materialized)
    # Always include the six public categories, including an honest zero.
    status_counts = {status: statuses[status] for status in B_STATUSES}
    a_safe = {"promote", "safe", "a_safe", "accepted"}

    def funnel(a_statuses: set[str]) -> dict[str, int]:
        subset = [row for row in materialized if _method_a_status(row) in a_statuses]
        return {
            "count": len(subset),
            "to_b_safe": sum(_status(row) == "b_safe" for row in subset),
            "to_b_usable": sum(_status(row) == "b_usable" for row in subset),
        }

    lifecycle: dict[str, dict[str, int]] = {}
    for row in materialized:
        name = _text(row.get("lifecycle")) or "unobserved"
        entry = lifecycle.setdefault(name, {"count": 0, "b_safe": 0, "b_usable": 0})
        entry["count"] += 1
        entry["b_safe"] += _status(row) == "b_safe"
        entry["b_usable"] += _status(row) == "b_usable"
    usable_reasons = Counter(
        reason for row in materialized if _status(row) == "b_usable" for reason in reason_codes(row)
    )
    invalid_usable = sorted(set(usable_reasons) - SOFT_USABILITY_REASONS)
    return {
        "rows": len(materialized),
        "assignment_counts": dict(sorted(assignment.items())),
        "method_b_status_counts": status_counts,
        "fallback_to_safe_usable": funnel({"fallback"}),
        "review_to_safe_usable": funnel({"review"}),
        "a_safe_control_to_safe_usable": funnel(a_safe),
        "lifecycle_safe_usable_counts": dict(sorted(lifecycle.items())),
        "comparison_metrics": comparison_metrics(materialized),
        "b_usable_reason_distribution": dict(sorted(usable_reasons.items())),
        "b_usable_reasons_all_soft": not invalid_usable,
        "b_usable_non_soft_reasons": invalid_usable,
    }


def _tracked(
    rows: Iterable[Mapping[str, Any]],
    suffixes: Iterable[str],
    *,
    audit_uid_to_entity_uid: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    found = {_uid(row): row for row in rows}
    results: dict[str, Any] = {}
    for suffix in suffixes:
        audit_matches = (
            [
                (audit_uid, entity_uid)
                for audit_uid, entity_uid in audit_uid_to_entity_uid.items()
                if audit_uid.endswith(suffix)
            ]
            if audit_uid_to_entity_uid is not None
            else []
        )
        if audit_uid_to_entity_uid is not None and len(audit_matches) != 1:
            results[suffix] = {
                "mapping": "unavailable" if not audit_matches else "ambiguous",
                "audit_uid_matches": len(audit_matches),
            }
            continue
        audit_uid, entity_uid = audit_matches[0] if audit_matches else (None, None)
        matches = (
            [(entity_uid, found[entity_uid])]
            if entity_uid is not None and entity_uid in found
            else [(uid, row) for uid, row in found.items() if uid.endswith(suffix)]
        )
        if len(matches) != 1:
            results[suffix] = {"mapping": "unavailable" if not matches else "ambiguous"}
            continue
        uid, row = matches[0]
        result = {
            "mapping": "matched",
            "source_row_uid": uid,
            "candidate_start": row.get("candidate_start", _boundary(row, "left", "method_b")),
            "candidate_end": row.get("candidate_end", _boundary(row, "right", "method_b")),
            "method_b_status": _status(row),
        }
        if audit_uid is not None:
            result["audit_uid"] = audit_uid
        results[suffix] = result
    return results


def boundary_usable_fix_comparison_report(
    before_rows: Iterable[Mapping[str, Any]],
    after_rows: Iterable[Mapping[str, Any]],
    *,
    expected_rows: int = 325,
    seed: int = 20260818,
    per_stratum: int = 25,
    tracked_uid_suffixes: Iterable[str] = TRACKED_UID_SUFFIXES,
    audit_uid_to_entity_uid: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Produce the immutable-pilot comparison artifact for this narrow fix."""
    before = [dict(row) for row in before_rows]
    after = [dict(row) for row in after_rows]
    before_uids, after_uids = {_uid(row) for row in before}, {_uid(row) for row in after}
    before_uids.discard("")
    after_uids.discard("")

    def digest(values: set[str]) -> str:
        return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()

    same = before_uids == after_uids and len(after_uids) == expected_rows
    suffixes = tuple(tracked_uid_suffixes)
    return {
        "pilot": {"seed": seed, "per_stratum": per_stratum, "expected_rows": expected_rows},
        "same_source_row_uid_set": "PASS" if same else "FAIL",
        "source_row_uid_sets": {
            "before_count": len(before_uids),
            "after_count": len(after_uids),
            "before_sha256": digest(before_uids),
            "after_sha256": digest(after_uids),
            "missing_after": sorted(before_uids - after_uids),
            "extra_after": sorted(after_uids - before_uids),
        },
        "assignment_limits_changed": False,
        "assignment_limits": {
            "actions": 10,
            "candidates": 30,
            "edges": 200,
            "states": 100_000,
        },
        "b_safe_semantics_changed": False,
        "before": _snapshot(before),
        "after": _snapshot(after),
        "tracked_boundary_cases": {
            "before": _tracked(before, suffixes, audit_uid_to_entity_uid=audit_uid_to_entity_uid),
            "after": _tracked(after, suffixes, audit_uid_to_entity_uid=audit_uid_to_entity_uid),
        },
    }
