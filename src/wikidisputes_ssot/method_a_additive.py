"""Monotonic, additive Method-A recovery hypothesis selection.

This module deliberately does not know how to fetch or parse revisions.  The
runner supplies the frozen baseline plus the current parser/ranker functions.
Keeping the selection seam small makes it possible to prove that a baseline
promotion is copied rather than recomputed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any

FALLBACK_TIERS = (
    "historical_signature_fallback",
    "certified_source_artifact",
    "historical_signature_certified_source_artifact",
    "legacy_candidate_current_safety",
)
FROZEN_PROMOTE_COUNT = 85_185


def _uid(row: Mapping[str, Any]) -> str:
    value = row.get("source_row_uid")
    if value in (None, ""):
        raise ValueError("row is missing source_row_uid")
    return str(value)


def _index(rows: Iterable[Mapping[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = _uid(row)
        if uid in result:
            raise ValueError(f"duplicate source_row_uid in {label}: {uid}")
        result[uid] = dict(row)
    return result


def _is_promoted(audit_row: Mapping[str, Any]) -> bool:
    return str(audit_row.get("decision") or "").casefold() == "promote"


def _as_csv_value(value: Any) -> str | None:
    """Match the baseline recovery artifact's all-string CSV representation."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    return str(value)


def _replacement(
    baseline: Mapping[str, Any],
    best: Mapping[str, Any],
    status: str,
    margin: float | None,
    tier: str,
    legacy_source_revision: str = "",
) -> dict[str, Any]:
    """Overlay only current rank evidence on one non-promoted baseline row."""
    row = dict(baseline)
    fields = {
        "recovery_status": status,
        "best_similarity": best.get("similarity"),
        "second_similarity": None,  # populated below from the selected rank pool
        "match_margin": margin,
        "offset_distance": best.get("offset_distance"),
        "raw_start": best.get("start"),
        "raw_end": best.get("end"),
        "boundary_method": best.get("boundary_method"),
        "target_coverage": best.get("target_coverage"),
        "candidate_purity": best.get("candidate_purity"),
        "normalized_length_delta": best.get("normalized_length_delta"),
        "signature_residue_detected": best.get("signature_residue_detected"),
        "hc_safety_reason": best.get("hc_safety_reason", ""),
        "source_signature_artifact_stripped": best.get("source_signature_artifact_stripped"),
        "source_signature_artifact_reason": best.get("source_signature_artifact_reason", ""),
        "source_original_similarity": best.get("source_original_similarity"),
        "source_original_target_coverage": best.get("source_original_target_coverage"),
        "source_original_candidate_purity": best.get("source_original_candidate_purity"),
        "source_original_length_delta": best.get("source_original_length_delta"),
        "recovered_body_wikitext": best.get("body_without_signature"),
        "recovered_raw_wikitext": best.get("raw"),
        "recovery_tier": tier,
        "candidate_provenance": best.get("provenance", "canonical_method_a"),
        "legacy_candidate_source_revision": legacy_source_revision,
        "source_comparison_mode": best.get("source_comparison_mode", "exact"),
    }
    row.update({key: _as_csv_value(value) for key, value in fields.items()})
    return row


def _tier(best: Mapping[str, Any]) -> str | None:
    historical = best.get("tier") == "historical_signature_fallback"
    artifact = best.get("source_comparison_mode") == "certified_source_artifact"
    if historical and artifact:
        return "historical_signature_certified_source_artifact"
    if historical:
        return "historical_signature_fallback"
    if artifact:
        return "certified_source_artifact"
    return None


def build_additive_rows(
    baseline_recovery: Iterable[Mapping[str, Any]],
    baseline_audit: Iterable[Mapping[str, Any]],
    revision_content: Callable[[int], tuple[str, str | None] | None],
    candidate_comments: Callable[[str, str | None], list[dict[str, Any]]],
    rank_candidates: Callable[[str, list[dict[str, Any]], int], list[dict[str, Any]]],
    classify: Callable[[dict[str, Any] | None, dict[str, Any] | None], tuple[str, float | None]],
    legacy_candidate_comments: Callable[
        [str, list[dict[str, Any]], str | None], list[dict[str, Any]]
    ],
    *,
    legacy_source_revision: str,
    expected_uid_count: int = 133_223,
    expected_promote_count: int = FROZEN_PROMOTE_COUNT,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return baseline-ordered rows, replacing only viable fallback hypotheses.

    A row whose baseline promotion audit says ``promote`` is appended before
    any revision lookup.  This is the monotonicity guarantee in executable
    form: none of its pre-existing keys is written by this function.
    """
    baseline = [dict(row) for row in baseline_recovery]
    recovery_by_uid = _index(baseline, "baseline recovery")
    audit_by_uid = _index(baseline_audit, "baseline promotion audit")
    if set(recovery_by_uid) != set(audit_by_uid):
        raise ValueError("baseline recovery and promotion audit UID sets differ")
    if len(baseline) != expected_uid_count:
        raise ValueError(f"baseline UID count={len(baseline):,}; expected {expected_uid_count:,}")
    promote_count = sum(_is_promoted(row) for row in audit_by_uid.values())
    if promote_count != expected_promote_count:
        raise ValueError(
            f"baseline promote count={promote_count:,}; expected {expected_promote_count:,}"
        )

    candidates_by_revision: dict[int, list[dict[str, Any]]] = {}
    legacy_by_revision: dict[int, list[dict[str, Any]]] = {}
    output: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()

    for original in baseline:
        uid = _uid(original)
        if _is_promoted(audit_by_uid[uid]):
            output.append(dict(original))
            counts["baseline_promote_preserved"] += 1
            continue

        try:
            revision_id = int(str(original.get("revision_id") or ""))
            action_offset = int(str(original.get("action_offset") or ""))
        except ValueError:
            output.append(dict(original))
            counts["baseline_retained"] += 1
            continue
        revision = revision_content(revision_id)
        if revision is None:
            output.append(dict(original))
            counts["baseline_retained"] += 1
            continue
        raw, expected_user = revision
        if revision_id not in candidates_by_revision:
            candidates_by_revision[revision_id] = candidate_comments(raw, expected_user)
        candidates = candidates_by_revision[revision_id]
        ranked = rank_candidates(str(original.get("source_text") or ""), candidates, action_offset)
        best = ranked[0] if ranked else None
        second = ranked[1] if len(ranked) > 1 else None
        status, margin = classify(best, second)
        selected_tier = _tier(best) if best is not None and status == "high_confidence" else None
        if selected_tier is not None:
            replacement = _replacement(original, best, status, margin, selected_tier)
            replacement["second_similarity"] = _as_csv_value(
                second.get("similarity") if second else None
            )
            output.append(replacement)
            counts[selected_tier] += 1
            continue

        # The current exact pool can still classify as high confidence for a
        # row that the separate promotion audit declined.  It is not an
        # unresolved fallback case, and therefore cannot be displaced by a
        # legacy hypothesis here.
        if status == "high_confidence":
            output.append(dict(original))
            counts["baseline_retained"] += 1
            continue

        # A frozen legacy region is considered only after every current tier
        # failed.  Its former label is never inspected: current classification
        # must independently reach high confidence.
        if revision_id not in legacy_by_revision:
            legacy_by_revision[revision_id] = legacy_candidate_comments(
                raw, candidates, expected_user
            )
        legacy_ranked = rank_candidates(
            str(original.get("source_text") or ""), legacy_by_revision[revision_id], action_offset
        )
        legacy_best = legacy_ranked[0] if legacy_ranked else None
        legacy_second = legacy_ranked[1] if len(legacy_ranked) > 1 else None
        legacy_status, legacy_margin = classify(legacy_best, legacy_second)
        if legacy_best is not None and legacy_status == "high_confidence":
            replacement = _replacement(
                original,
                legacy_best,
                legacy_status,
                legacy_margin,
                "legacy_candidate_current_safety",
                legacy_source_revision,
            )
            replacement["second_similarity"] = _as_csv_value(
                legacy_second.get("similarity") if legacy_second else None
            )
            output.append(replacement)
            counts["legacy_candidate_current_safety"] += 1
            continue
        output.append(dict(original))
        counts["baseline_retained"] += 1

    if [_uid(row) for row in output] != [_uid(row) for row in baseline]:
        raise AssertionError("additive runner changed baseline recovery row order")
    return output, dict(counts)
