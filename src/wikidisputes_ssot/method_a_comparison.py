"""Deterministic before/after accounting for Method-A promotion artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

import pyarrow.parquet as pq

DENOMINATOR = 133_223
PROMOTION_DECISIONS = {"promote"}
VALID_METHOD_B_STATUSES = {"b_safe", "b_usable"}
RECOVERY_TIERS = (
    "historical_signature_fallback",
    "certified_source_artifact",
    "historical_signature_certified_source_artifact",
    "legacy_candidate_current_safety",
)


class HardRegressionError(RuntimeError):
    """Raised when a previous promotion is lost or its exact evidence changes."""


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


def _require_same_uids(label: str, *indexes: Mapping[str, Any]) -> set[str]:
    sets = [set(index) for index in indexes]
    first = sets[0]
    if any(value != first for value in sets[1:]):
        differences = [
            {"missing": sorted(first - value)[:5], "extra": sorted(value - first)[:5]}
            for value in sets[1:]
        ]
        raise ValueError(f"source_row_uid sets differ for {label}: {differences}")
    return first


def _decision(row: Mapping[str, Any]) -> str:
    return _text(row.get("decision")).casefold()


def _is_promoted(row: Mapping[str, Any]) -> bool:
    return _decision(row) in PROMOTION_DECISIONS


def _bool(value: Any) -> bool:
    return value is True or _text(value).strip().casefold() in {"1", "true", "yes"}


def recovery_tier(row: Mapping[str, Any]) -> str:
    """Read an explicit tier when present, otherwise infer the stored legacy schema."""
    explicit = _text(row.get("tier") or row.get("recovery_tier"))
    if explicit in RECOVERY_TIERS:
        return explicit
    boundary = _text(row.get("boundary_method"))
    provenance = _text(row.get("provenance"))
    if boundary == "historical_signature_fallback" or provenance.startswith(
        "historical_signature_fallback"
    ):
        return "historical_signature_fallback"
    if boundary == "legacy_timestamp_region_hypothesis" or provenance.startswith(
        "legacy_candidate_current_safety"
    ):
        return "legacy_candidate_current_safety"
    if (
        _bool(row.get("source_signature_artifact_stripped"))
        or _text(row.get("source_comparison_mode")) == "certified_source_artifact"
    ):
        return "certified_source_artifact"
    return explicit or "existing_current"


def _exact_value(row: Mapping[str, Any], name: str) -> Any:
    """Return raw values without normalization; absent and null remain distinct."""
    return row.get(name) if name in row else {"missing": True}


def _sha256(value: Any) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _change(uid: str, field: str, before: Any, after: Any) -> dict[str, Any]:
    return {
        "source_row_uid": uid,
        "field": field,
        "before": before,
        "after": after,
        "before_sha256": _sha256(before),
        "after_sha256": _sha256(after),
    }


def compare_method_a(
    baseline_recovery: Iterable[Mapping[str, Any]],
    baseline_audit: Iterable[Mapping[str, Any]],
    current_recovery: Iterable[Mapping[str, Any]],
    current_audit: Iterable[Mapping[str, Any]],
    method_b_evidence: Iterable[Mapping[str, Any]],
    *,
    denominator: int = DENOMINATOR,
    raise_on_regression: bool = True,
) -> dict[str, Any]:
    """Compare complete, UID-aligned artifacts without normalizing source evidence."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    old_recovery = _index(baseline_recovery, "baseline recovery")
    old_audit = _index(baseline_audit, "baseline promotion audit")
    new_recovery = _index(current_recovery, "current recovery")
    new_audit = _index(current_audit, "current promotion audit")
    method_b = _index(method_b_evidence, "Method-B evidence")
    uids = _require_same_uids(
        "baseline/current recovery and promotion audit",
        old_recovery,
        old_audit,
        new_recovery,
        new_audit,
    )

    old_promoted = {uid for uid in uids if _is_promoted(old_audit[uid])}
    new_promoted = {uid for uid in uids if _is_promoted(new_audit[uid])}
    newly_promoted = new_promoted - old_promoted
    lost = old_promoted - new_promoted

    tier_counts = {tier: 0 for tier in RECOVERY_TIERS}
    unclassified_tiers: dict[str, int] = {}
    for uid in newly_promoted:
        tier = recovery_tier(new_recovery[uid])
        if tier in tier_counts:
            tier_counts[tier] += 1
        else:  # Defensive for future explicit tiers.
            unclassified_tiers[tier] = unclassified_tiers.get(tier, 0) + 1

    regressions: dict[str, list[dict[str, Any]]] = {
        "lost_promotions": [{"source_row_uid": uid} for uid in sorted(lost)],
        "raw_interval_changes": [],
        "raw_text_changes": [],
        "body_text_changes": [],
        "status_changes": [],
    }
    for uid in sorted(old_promoted & new_promoted):
        before, after = old_recovery[uid], new_recovery[uid]
        old_interval = (_exact_value(before, "raw_start"), _exact_value(before, "raw_end"))
        new_interval = (_exact_value(after, "raw_start"), _exact_value(after, "raw_end"))
        if old_interval != new_interval:
            regressions["raw_interval_changes"].append(
                _change(uid, "raw_interval", old_interval, new_interval)
            )
        old_raw = _exact_value(before, "recovered_raw_wikitext")
        new_raw = _exact_value(after, "recovered_raw_wikitext")
        if old_raw != new_raw:
            regressions["raw_text_changes"].append(
                _change(uid, "recovered_raw_wikitext", old_raw, new_raw)
            )
        old_body = _exact_value(before, "recovered_body_wikitext")
        new_body = _exact_value(after, "recovered_body_wikitext")
        if old_body != new_body:
            regressions["body_text_changes"].append(
                _change(uid, "recovered_body_wikitext", old_body, new_body)
            )
        old_status, new_status = (
            _exact_value(before, "recovery_status"),
            _exact_value(after, "recovery_status"),
        )
        if old_status != new_status:
            regressions["status_changes"].append(
                _change(uid, "recovery_status", old_status, new_status)
            )

    b_validated = {
        uid
        for uid, row in method_b.items()
        if uid in uids and _text(row.get("status")).casefold() in VALID_METHOD_B_STATUSES
    }
    new_a_b_overlap = newly_promoted & b_validated
    new_unique_a = newly_promoted - b_validated
    unique_validated = new_promoted | b_validated
    regression_uids = {
        change["source_row_uid"] for values in regressions.values() for change in values
    }
    report = {
        "denominator": denominator,
        "uid_count": len(uids),
        "method_a_decisions": {
            "before": {
                decision: sum(_decision(row) == decision for row in old_audit.values())
                for decision in ("promote", "review", "fallback")
            },
            "after": {
                decision: sum(_decision(row) == decision for row in new_audit.values())
                for decision in ("promote", "review", "fallback")
            },
        },
        "new_promotions": {
            "count": len(newly_promoted),
            "by_recovery_tier": tier_counts,
            "unclassified_tiers": dict(sorted(unclassified_tiers.items())),
        },
        "regressions": {
            **regressions,
            "hard_regression_uid_count": len(regression_uids),
            "hard_regression_count": sum(len(value) for value in regressions.values()),
        },
        "method_a_method_b": {
            "method_b_safe_or_b_usable": len(b_validated),
            "new_a_promotions_overlapping_b_safe_or_b_usable": len(new_a_b_overlap),
            "new_unique_a_gains": len(new_unique_a),
            "total_unique_validated_a_plus_b": len(unique_validated),
            "total_unique_validated_a_plus_b_percentage": len(unique_validated) * 100 / denominator,
        },
    }
    if raise_on_regression and report["regressions"]["hard_regression_count"]:
        raise HardRegressionError("hard Method-A regression(s) detected")
    return report


def read_parquet_rows(path: str) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist()
