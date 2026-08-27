"""Pure, deterministic reporting helpers for revision-diff recovery work.

The helpers deliberately accept ordinary mappings.  They are intended to be
called by a storage/CLI layer, but neither read nor write project artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..promotion_safety import comparison_tokens, visible_text


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _bool(value: Any) -> bool:
    return value is True or _text(value).strip().casefold() in {"1", "true", "yes"}


def _first(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _first_present(row: Mapping[str, Any], *names: str) -> Any:
    """Return the first present value, preserving an explicitly empty string.

    Validation must distinguish unavailable text from an available empty body.
    Most reporting call sites intentionally use :func:`_first`, whose empty
    string skipping behavior remains useful for display-oriented fallbacks.
    """
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _uid(row: Mapping[str, Any]) -> str:
    return _text(
        _first(
            row,
            "source_row_uid",
            "revision_diff_uid",
            "action_uid",
            "utterance_action_uid",
            "id",
            "uid",
        )
    )


def _stable_hash(seed: int | str, *parts: Any) -> str:
    payload = json.dumps([seed, *parts], ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _status(row: Mapping[str, Any]) -> str:
    return (
        _text(
            _first(row, "method_a_status", "promotion_decision", "safety_decision", "status")
        ).casefold()
        or "unobserved"
    )


def _reasons(row: Mapping[str, Any]) -> list[str]:
    raw = _first(row, "method_a_reasons", "promotion_reasons", "safety_reasons", "reasons")
    if isinstance(raw, (list, tuple, set)):
        return sorted({_text(item) for item in raw if _text(item)})
    if isinstance(raw, str) and raw.lstrip().startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return sorted({_text(item) for item in parsed if _text(item)})
    return sorted({part.strip() for part in _text(raw).split("|") if part.strip()})


def _action_class(row: Mapping[str, Any]) -> str:
    value = _text(
        _first(row, "action_type", "action_class", "change_type", "revision_action", "diff_type")
    ).casefold()
    if value in {"creation", "original", "add", "addition", "insert", "new"}:
        return "addition"
    if value in {"restore", "restoration", "revert_restore"}:
        return "restoration"
    if value in {"delete", "deletion", "remove"}:
        return "deletion"
    return "modification" if value else "unclassified"


def _strata(row: Mapping[str, Any]) -> list[str]:
    """Return overlapping reporting strata; a row may be in several buckets."""
    result: list[str] = []
    status = _status(row)
    result.append(f"method_a:{status}")
    method_b_status = _text(
        _first(row, "method_b_status", "revision_diff_status", "status")
    ).casefold()
    if method_b_status:
        result.append(f"method_b:{method_b_status}")
    if status == "fallback" or _bool(row.get("used_fallback")):
        result.append("fallback")
    if status == "review":
        result.append("review")
    if status in {"fallback", "review"} or _bool(row.get("used_fallback")):
        result.append("fallback_or_review")
    if status in {"fallback", "review"} and method_b_status == "b_safe":
        result.append(f"{status}_to_b_safe")
    if method_b_status and method_b_status != "b_safe":
        result.append("method_b_unresolved")
    if _bool(_first(row, "empty_target", "target_empty")) or not _text(
        _first(row, "target_text", "trusted_text", "anchor_text")
    ):
        result.append("empty_target")
    result.append(_action_class(row))
    if (
        _bool(_first(row, "multi_action_revision", "revision_is_multi_action"))
        or int(_first(row, "action_count_in_revision", "revision_action_count") or 1) > 1
    ):
        result.append("multi_action")
    difficult = _bool(_first(row, "difficult", "is_difficult")) or bool(_reasons(row))
    if difficult:
        result.append("difficult")
    if status in {"promote", "safe", "a_safe", "accepted"}:
        result.append("a_safe_control")
    return sorted(set(result))


def profile_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project source rows into an explicit, review-oriented profile."""
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        reasons = _reasons(row)
        target = _text(_first(row, "target_text", "trusted_text", "anchor_text"))
        candidate = _text(_first(row, "candidate_raw_body", "candidate_text", "recovered_text"))
        markup = _first(row, "markup_density", "candidate_markup_density")
        if markup in (None, ""):
            markers = candidate.count("[") + candidate.count("{") + candidate.count("<")
            markup = markers / max(1, len(candidate))
        target_length = len(target)
        length_bucket = (
            "empty"
            if target_length == 0
            else "1_49"
            if target_length < 50
            else "50_199"
            if target_length < 200
            else "200_999"
            if target_length < 1000
            else "1000_plus"
        )
        markup_value = float(markup)
        markup_bucket = (
            "none" if markup_value == 0 else "sparse" if markup_value < 0.02 else "dense"
        )
        output.append(
            {
                "entity_uid": _uid(row),
                "method_a_status": _status(row),
                "method_a_reasons": reasons,
                "fallback_or_review": _status(row) in {"fallback", "review"}
                or _bool(row.get("used_fallback")),
                "lifecycle": _text(_first(row, "action_type", "lifecycle", "lifecycle_status"))
                or "unobserved",
                "revision_available": _bool(
                    _first(
                        row,
                        "revision_available",
                        "revision_content_available",
                        "has_revision",
                    )
                ),
                "target_availability": _text(
                    _first(row, "target_availability", "revision_availability")
                )
                or "unobserved",
                "predecessor_availability": _text(row.get("predecessor_availability"))
                or "unobserved",
                "predecessor_available": _bool(
                    _first(row, "predecessor_available", "has_predecessor")
                ),
                "candidate_available": bool(candidate),
                "year": _first(row, "year", "revision_year"),
                "target_length": target_length,
                "target_length_bucket": length_bucket,
                "candidate_length": len(candidate),
                "markup_density": markup_value,
                "markup_density_bucket": markup_bucket,
                "empty_target": not bool(target),
                "multi_action_revision": "multi_action" in _strata(row),
                "action_class": _action_class(row),
                "strata": _strata(row),
            }
        )
    return sorted(output, key=lambda item: item["entity_uid"])


def select_stratified_pilot(
    rows: Iterable[Mapping[str, Any]], *, seed: int | str = 0, per_stratum: int = 5
) -> dict[str, Any]:
    """Select each stratum independently, with deterministic overlapping membership."""
    if per_stratum < 0:
        raise ValueError("per_stratum must be non-negative")
    source = [dict(row) for row in rows]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        for stratum in _strata(row):
            grouped[stratum].append(row)
    selected: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stratum in sorted(grouped):
        members = sorted(
            grouped[stratum], key=lambda row: (_stable_hash(seed, stratum, _uid(row)), _uid(row))
        )
        chosen = members[:per_stratum]
        manifest.append(
            {
                "stratum": stratum,
                "population_count": len(members),
                "selected_count": len(chosen),
                "seed": seed,
            }
        )
        for row in chosen:
            uid = _uid(row)
            if uid not in seen:
                selected.append(dict(row, pilot_strata=_strata(row)))
                seen.add(uid)
    return {
        "seed": seed,
        "per_stratum": per_stratum,
        "rows": sorted(selected, key=lambda row: _uid(row)),
        "strata_manifest": manifest,
    }


def _critical_tokens(tokens: Iterable[str]) -> list[str]:
    """Keep the high-consequence token classes visible in validation output."""
    negations = {"not", "n't", "never", "no", "none", "cannot", "without"}
    operators = {"%", "+", "*", "/", "^", "=", "==", "!=", "<", ">", "<=", ">="}
    return sorted(
        {
            token
            for token in tokens
            if token in negations
            or token in operators
            or token.startswith(("http://", "https://"))
            or any(char.isdigit() for char in token)
        }
    )


def _comparison(
    left: Any,
    right: Any,
    *,
    left_present: bool | None = None,
    right_present: bool | None = None,
) -> dict[str, Any]:
    left_present = left is not None if left_present is None else left_present
    right_present = right is not None if right_present is None else right_present
    comparable = left_present and right_present
    raw_left = _text(left) if left_present else None
    raw_right = _text(right) if right_present else None
    visible_left = visible_text(raw_left) if comparable else None
    visible_right = visible_text(raw_right) if comparable else None
    left_tokens = comparison_tokens(raw_left) if comparable else None
    right_tokens = comparison_tokens(raw_right) if comparable else None
    return {
        "left_present": left_present,
        "right_present": right_present,
        "comparable": comparable,
        "raw_equal": raw_left == raw_right if comparable else None,
        "visible_equal": visible_left == visible_right if comparable else None,
        "raw_left": raw_left,
        "raw_right": raw_right,
        "visible_left": visible_left,
        "visible_right": visible_right,
        "left_tokens": left_tokens,
        "right_tokens": right_tokens,
        "missing_critical_tokens": (
            _critical_tokens(set(left_tokens) - set(right_tokens)) if comparable else None
        ),
        "added_critical_tokens": (
            _critical_tokens(set(right_tokens) - set(left_tokens)) if comparable else None
        ),
    }


def _boundary_comparison(left: Any, right: Any) -> tuple[bool, bool | None]:
    comparable = left is not None and right is not None
    return comparable, left == right if comparable else None


def _contamination_status(value: Any) -> str:
    """Return only measured contamination states; legacy booleans are unknown."""
    status = _text(value).strip().casefold()
    return status if status in {"unknown", "clean", "detected"} else "unknown"


def _agreement_metrics(
    rows: Iterable[Mapping[str, Any]], *, comparable_key: str, equal_key: str
) -> dict[str, int | float | None]:
    members = list(rows)
    comparable_count = sum(bool(row[comparable_key]) for row in members)
    agreement_count = sum(row[equal_key] is True for row in members)
    return {
        "comparable_count": comparable_count,
        "agreement_count": agreement_count,
        "agreement_rate": agreement_count / comparable_count if comparable_count else None,
    }


def _critical_token_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, int | None]:
    members = list(rows)
    comparable_count = sum(item["critical_token_difference"] is not None for item in members)
    return {
        "comparable_count": comparable_count,
        "difference_count": sum(item["critical_token_difference"] is True for item in members),
    }


def _contamination_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(item["method_b_contamination_status"] for item in rows)
    return {
        "evaluated_count": counts["clean"] + counts["detected"],
        "detected_count": counts["detected"],
        "unknown_count": counts["unknown"],
    }


def _report_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    members = list(rows)
    raw = _agreement_metrics(members, comparable_key="text_comparable", equal_key="raw_body_equal")
    visible = _agreement_metrics(
        members, comparable_key="text_comparable", equal_key="visible_text_equal"
    )
    left = _agreement_metrics(
        members, comparable_key="left_boundary_comparable", equal_key="left_boundary_equal"
    )
    right = _agreement_metrics(
        members, comparable_key="right_boundary_comparable", equal_key="right_boundary_equal"
    )
    critical = _critical_token_metrics(members)
    contamination = _contamination_metrics(members)
    return {
        "raw_body_comparable_count": raw["comparable_count"],
        "raw_body_agreement_count": raw["agreement_count"],
        "raw_body_agreement_rate": raw["agreement_rate"],
        "visible_text_comparable_count": visible["comparable_count"],
        "visible_text_agreement_count": visible["agreement_count"],
        "visible_text_agreement_rate": visible["agreement_rate"],
        "left_boundary_comparable_count": left["comparable_count"],
        "left_boundary_agreement_count": left["agreement_count"],
        "left_boundary_agreement_rate": left["agreement_rate"],
        "right_boundary_comparable_count": right["comparable_count"],
        "right_boundary_agreement_count": right["agreement_count"],
        "right_boundary_agreement_rate": right["agreement_rate"],
        "critical_token_comparable_count": critical["comparable_count"],
        "critical_token_difference_count": critical["difference_count"],
        "contamination_evaluated_count": contamination["evaluated_count"],
        "contamination_detected_count": contamination["detected_count"],
        "contamination_unknown_count": contamination["unknown_count"],
        # Established count names remain valid, now with explicit denominators.
        "exact_raw_body_agreement_count": raw["agreement_count"],
        "adjacent_contamination_count": contamination["detected_count"],
    }


def pilot_validation_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Describe A/B agreement without treating Method A as truth."""
    out: list[dict[str, Any]] = []
    for row in rows:
        source = dict(row)
        a = _first_present(
            source, "method_a_candidate_raw_body", "candidate_raw_body", "method_a_candidate"
        )
        b = _first_present(
            source,
            "method_b_candidate_raw_body",
            "control_candidate_raw_body",
            "method_b_candidate",
        )
        method_a_available = _first_present(
            source, "method_a_candidate_available", "candidate_available"
        )
        method_b_available = _first_present(
            source, "method_b_candidate_available", "control_candidate_available"
        )
        comparison = _comparison(
            a,
            b,
            left_present=(
                _bool(method_a_available) if method_a_available is not None else a is not None
            ),
            right_present=(
                _bool(method_b_available) if method_b_available is not None else b is not None
            ),
        )
        left_a = _first_present(source, "method_a_left_boundary", "left_boundary")
        left_b = _first_present(source, "method_b_left_boundary", "control_left_boundary")
        right_a = _first_present(source, "method_a_right_boundary", "right_boundary")
        right_b = _first_present(source, "method_b_right_boundary", "control_right_boundary")
        left_boundary_comparable, left_boundary_equal = _boundary_comparison(left_a, left_b)
        right_boundary_comparable, right_boundary_equal = _boundary_comparison(right_a, right_b)
        method_a_contamination = _contamination_status(
            _first_present(source, "method_a_contamination", "contamination")
        )
        method_b_contamination = _contamination_status(
            _first_present(
                source,
                "method_b_contamination",
                "neighboring_comment_contamination",
                "control_contamination",
            )
        )
        critical_token_difference = (
            bool(comparison["missing_critical_tokens"] or comparison["added_critical_tokens"])
            if comparison["comparable"]
            else None
        )
        out.append(
            {
                "entity_uid": _uid(source),
                "comparison_reference": "method_a_not_ground_truth",
                "method_a_status": _status(source),
                "method_b_status": _text(
                    _first(source, "method_b_status", "revision_diff_status", "status")
                )
                or "unobserved",
                "candidate": comparison,
                "text_comparable": comparison["comparable"],
                "raw_body_equal": comparison["raw_equal"],
                "visible_text_equal": comparison["visible_equal"],
                "critical_token_difference": critical_token_difference,
                "left_boundaries": {"method_a": left_a, "method_b": left_b},
                "right_boundaries": {"method_a": right_a, "method_b": right_b},
                "left_boundary_comparable": left_boundary_comparable,
                "right_boundary_comparable": right_boundary_comparable,
                "left_boundary_equal": left_boundary_equal,
                "right_boundary_equal": right_boundary_equal,
                "method_a_contamination_status": method_a_contamination,
                "method_b_contamination_status": method_b_contamination,
                "contamination_agree": (
                    method_a_contamination == method_b_contamination
                    if method_a_contamination != "unknown" and method_b_contamination != "unknown"
                    else None
                ),
                "assignment_ambiguity_agree": _first(
                    source, "method_a_assignment_ambiguity", "assignment_ambiguity"
                )
                == _first(
                    source,
                    "method_b_assignment_ambiguity",
                    "control_assignment_ambiguity",
                ),
                "method_b_adjacent_contamination": method_b_contamination == "detected",
                "method_b_assignment_ambiguous": _bool(
                    _first(
                        source,
                        "method_b_assignment_ambiguity",
                        "assignment_ambiguity",
                    )
                ),
                "lifecycle": _text(_first(source, "action_type", "lifecycle", "lifecycle_status"))
                or "unobserved",
            }
        )
    return sorted(out, key=lambda item: item["entity_uid"])


def pilot_validation_report(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Package comparison rows with lifecycle-level agreement yields."""
    comparisons = pilot_validation_rows(rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in comparisons:
        grouped[row["lifecycle"]].append(row)
    lifecycle_yields = []
    for lifecycle in sorted(grouped):
        members = grouped[lifecycle]
        safe_count = sum(item["method_b_status"] == "b_safe" for item in members)
        lifecycle_yields.append(
            {
                "lifecycle": lifecycle,
                "comparison_count": len(members),
                **_report_metrics(members),
                "assignment_ambiguity_count": sum(
                    item["method_b_assignment_ambiguous"] for item in members
                ),
                "method_b_safe_count": safe_count,
                "method_b_safe_yield": safe_count / len(members),
            }
        )
    controls = [
        row
        for row in comparisons
        if row["method_a_status"] in {"promote", "safe", "a_safe", "accepted"}
    ]
    return {
        "rows": comparisons,
        "lifecycle_yields": lifecycle_yields,
        "overall_agreement": {
            "comparison_count": len(comparisons),
            **_report_metrics(comparisons),
            "assignment_ambiguity_count": sum(
                row["method_b_assignment_ambiguous"] for row in comparisons
            ),
        },
        "method_a_safe_controls": {
            "comparison_count": len(controls),
            **_report_metrics(controls),
        },
        "status_pairs": [
            {"method_a_status": pair[0], "method_b_status": pair[1], "count": count}
            for pair, count in sorted(
                Counter(
                    (row["method_a_status"], row["method_b_status"]) for row in comparisons
                ).items()
            )
        ],
    }


def _truthy(value: Any) -> bool:
    return value is True or _text(value).strip().casefold() in {"1", "true", "yes"}


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _distribution(values: Iterable[Any]) -> dict[str, int | float | None]:
    ordered = sorted(_integer(value) for value in values if value is not None and value != "")
    if not ordered:
        return {"median": None, "p90": None, "max": None}
    middle = len(ordered) // 2
    median_value: int | float = (
        ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    )
    if isinstance(median_value, float) and median_value.is_integer():
        median_value = int(median_value)
    return {
        "median": median_value,
        "p90": ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)],
        "max": ordered[-1],
    }


def _comparison_metrics_from_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    raw_comparable = sum(
        _truthy(row.get("a_body_present")) and _truthy(row.get("b_body_present")) for row in rows
    )
    visible_comparable = raw_comparable
    left_comparable = sum(
        row.get("a_left") not in (None, "") and row.get("b_left") not in (None, "") for row in rows
    )
    right_comparable = sum(
        row.get("a_right") not in (None, "") and row.get("b_right") not in (None, "")
        for row in rows
    )
    raw_agreement = sum(
        _truthy(row.get("exact_body_equal"))
        and _truthy(row.get("a_body_present"))
        and _truthy(row.get("b_body_present"))
        for row in rows
    )
    visible_agreement = sum(
        _truthy(row.get("visible_body_equal"))
        and _truthy(row.get("a_body_present"))
        and _truthy(row.get("b_body_present"))
        for row in rows
    )
    left_agreement = sum(
        _truthy(row.get("left_boundary_equal"))
        and row.get("a_left") not in (None, "")
        and row.get("b_left") not in (None, "")
        for row in rows
    )
    right_agreement = sum(
        _truthy(row.get("right_boundary_equal"))
        and row.get("a_right") not in (None, "")
        and row.get("b_right") not in (None, "")
        for row in rows
    )

    def metric(comparable: int, agreement: int) -> dict[str, int | float | None]:
        return {
            "comparable_count": comparable,
            "agreement_count": agreement,
            "agreement_rate": agreement / comparable if comparable else None,
        }

    return {
        "raw_body": metric(raw_comparable, raw_agreement),
        "visible_text": metric(visible_comparable, visible_agreement),
        "left_boundary": metric(left_comparable, left_agreement),
        "right_boundary": metric(right_comparable, right_agreement),
        "critical_tokens": {
            "comparable_count": raw_comparable,
            "difference_count": sum(
                _truthy(row.get("critical_token_difference"))
                and _truthy(row.get("a_body_present"))
                and _truthy(row.get("b_body_present"))
                for row in rows
            ),
        },
    }


def _snapshot_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    validation_metrics: Mapping[str, Any] | None = None,
    baseline: bool = False,
) -> dict[str, Any]:
    assignment_counts: Counter[str] = Counter()
    ambiguity_reasons: Counter[str] = Counter()
    single_total = single_ambiguous = multi_total = multi_ambiguous = 0
    method_b_status = Counter(
        _text(row.get("method_b_status") or row.get("status")) for row in rows
    )
    for row in rows:
        status = _text(row.get("assignment_status"))
        if not status or status == "not_applicable":
            status = "missing"
        assignment_counts[status] += 1
        ambiguous = status == "ambiguous"
        multi = (
            _truthy(row.get("multi_action_effective"))
            or _integer(row.get("action_count", row.get("effective_action_count", 1)), 1) > 1
        )
        if multi:
            multi_total += 1
            multi_ambiguous += ambiguous
        else:
            single_total += 1
            single_ambiguous += ambiguous
        if ambiguous:
            raw_reasons = row.get("assignment_conflicts_json", row.get("assignment_conflicts"))
            ambiguity_reasons.update(_json_reasons(raw_reasons))

    whole_counts = [
        row.get("whole_page_candidate_count", row.get("candidate_count")) for row in rows
    ]
    localized_counts = [] if baseline else [row.get("localized_candidate_count") for row in rows]
    zero_localized = sum(
        _integer(row.get("localized_candidate_count", row.get("candidate_count"))) == 0
        and _text(row.get("assignment_status")) not in {"", "missing", "not_applicable"}
        and _text(row.get("method_b_status") or row.get("status")) != "b_unavailable"
        for row in rows
    )
    assignment_outcomes = {
        "zero_localized_structural_candidates": zero_localized,
        "candidates_exist_assignment_ambiguous": sum(
            _integer(row.get("localized_candidate_count", row.get("candidate_count"))) > 0
            and _text(row.get("assignment_status")) == "ambiguous"
            for row in rows
        ),
        "candidates_exist_but_unmatched": sum(
            _integer(row.get("localized_candidate_count", row.get("candidate_count"))) > 0
            and _text(row.get("assignment_status")) == "unmatched"
            for row in rows
        ),
        "assigned": assignment_counts["assigned"],
        "unavailable": method_b_status["b_unavailable"],
    }
    a_safe = [row for row in rows if _text(row.get("method_a_status")) == "promote"]
    a_safe_funnel = {
        "count": len(a_safe),
        "whole_page_structural_candidate_exists": sum(
            _integer(row.get("whole_page_candidate_count", row.get("candidate_count"))) > 0
            for row in a_safe
        ),
        "localized_structural_candidate_exists": (
            None
            if baseline
            else sum(_integer(row.get("localized_candidate_count")) > 0 for row in a_safe)
        ),
        "assignment_unique": sum(
            _text(row.get("assignment_status")) == "assigned" for row in a_safe
        ),
        "b_safe": sum(
            _text(row.get("method_b_status") or row.get("status")) == "b_safe" for row in a_safe
        ),
    }
    contamination = Counter(
        "unknown"
        if baseline
        else _contamination_status(row.get("neighboring_comment_contamination"))
        for row in rows
    )
    corrected = (
        dict(validation_metrics)
        if validation_metrics is not None
        else _comparison_metrics_from_rows(rows)
    )
    return {
        "rows": len(rows),
        "assignment_status": dict(sorted(assignment_counts.items())),
        "ambiguity_reasons": dict(sorted(ambiguity_reasons.items())),
        "ambiguity_by_revision_multiplicity": {
            "single_action": {"count": single_total, "ambiguous": single_ambiguous},
            "multi_action": {"count": multi_total, "ambiguous": multi_ambiguous},
        },
        "candidate_distributions": {
            "whole_page": _distribution(whole_counts),
            "localized": _distribution(localized_counts),
        },
        "rows_with_more_than_30_localized_candidates": (
            None
            if baseline
            else sum(_integer(row.get("localized_candidate_count")) > 30 for row in rows)
        ),
        "resource_guard_decomposition": {
            "revision_too_large_rows": ambiguity_reasons[
                "revision_too_large_for_safe_global_assignment"
            ],
            "localized_candidate_limit_rows": (
                None
                if baseline
                else sum(
                    _integer(row.get("localized_candidate_count")) > 30
                    and "revision_too_large_for_safe_global_assignment"
                    in _json_reasons(row.get("assignment_conflicts_json"))
                    for row in rows
                )
            ),
            "other_active_graph_limit_rows": (
                None
                if baseline
                else ambiguity_reasons["revision_too_large_for_safe_global_assignment"]
                - sum(
                    _integer(row.get("localized_candidate_count")) > 30
                    and "revision_too_large_for_safe_global_assignment"
                    in _json_reasons(row.get("assignment_conflicts_json"))
                    for row in rows
                )
            ),
        },
        "method_b_status": dict(sorted(method_b_status.items())),
        "assignment_outcomes": assignment_outcomes,
        "fallback_to_b_safe": sum(
            _text(row.get("method_a_status")) == "fallback"
            and _text(row.get("method_b_status") or row.get("status")) == "b_safe"
            for row in rows
        ),
        "review_to_b_safe": sum(
            _text(row.get("method_a_status")) == "review"
            and _text(row.get("method_b_status") or row.get("status")) == "b_safe"
            for row in rows
        ),
        "a_safe_control_funnel": a_safe_funnel,
        "corrected_validation": corrected,
        "contamination": {
            "evaluated": contamination["clean"] + contamination["detected"],
            "detected": contamination["detected"],
            "unknown": contamination["unknown"],
        },
    }


def localization_fix_comparison_report(
    before_rows: Iterable[Mapping[str, Any]],
    after_rows: Iterable[Mapping[str, Any]],
    *,
    validation_report: Mapping[str, Any],
    expected_rows: int = 325,
    seed: int = 20260818,
    per_stratum: int = 25,
) -> dict[str, Any]:
    """Build the frozen-pilot before/after artifact for the localization fix."""

    before = [dict(row) for row in before_rows]
    after = [dict(row) for row in after_rows]
    before_uids = {_uid(row) for row in before}
    after_uids = {_uid(row) for row in after}
    same = before_uids == after_uids and len(after_uids) == expected_rows

    def uid_hash(values: set[str]) -> str:
        return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()

    overall = validation_report.get("overall_agreement", {})
    after_validation = {
        "raw_body": {
            "comparable_count": overall.get("raw_body_comparable_count"),
            "agreement_count": overall.get("raw_body_agreement_count"),
            "agreement_rate": overall.get("raw_body_agreement_rate"),
        },
        "visible_text": {
            "comparable_count": overall.get("visible_text_comparable_count"),
            "agreement_count": overall.get("visible_text_agreement_count"),
            "agreement_rate": overall.get("visible_text_agreement_rate"),
        },
        "left_boundary": {
            "comparable_count": overall.get("left_boundary_comparable_count"),
            "agreement_count": overall.get("left_boundary_agreement_count"),
            "agreement_rate": overall.get("left_boundary_agreement_rate"),
        },
        "right_boundary": {
            "comparable_count": overall.get("right_boundary_comparable_count"),
            "agreement_count": overall.get("right_boundary_agreement_count"),
            "agreement_rate": overall.get("right_boundary_agreement_rate"),
        },
        "critical_tokens": {
            "comparable_count": overall.get("critical_token_comparable_count"),
            "difference_count": overall.get("critical_token_difference_count"),
        },
    }
    return {
        "pilot": {"seed": seed, "per_stratum": per_stratum, "expected_rows": expected_rows},
        "same_source_row_uid_set": "PASS" if same else "FAIL",
        "source_row_uid_sets": {
            "before_count": len(before_uids),
            "after_count": len(after_uids),
            "before_sha256": uid_hash(before_uids),
            "after_sha256": uid_hash(after_uids),
            "missing_after": sorted(before_uids - after_uids),
            "extra_after": sorted(after_uids - before_uids),
        },
        "assignment_limits_changed": False,
        "before": _snapshot_metrics(before, baseline=True),
        "after": _snapshot_metrics(after, validation_metrics=after_validation),
        "validation_by_lifecycle": validation_report.get("lifecycle_yields", []),
    }


def recovery_report(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return accounting counts/yields, not interpretive or empirical claims."""
    materialized = [dict(row) for row in rows]
    total = len(materialized)
    recovered = [
        row
        for row in materialized
        if _text(_first(row, "method_b_status", "status"))
        in {"promote", "safe", "b_safe", "accepted", "recovered"}
        or _bool(row.get("recovered"))
    ]
    categories: Counter[str] = Counter()
    additions: Counter[str] = Counter()
    for row in recovered:
        raw = _first(row, "recovered_markup_categories", "markup_categories")
        values = raw if isinstance(raw, (list, tuple, set)) else _text(raw).split("|")
        categories.update(_text(value) for value in values if _text(value))
        raw_additions = row.get("recovered_markup_additions")
        if isinstance(raw_additions, Mapping):
            additions.update(
                {
                    _text(name): int(count)
                    for name, count in raw_additions.items()
                    if _text(name) and int(count) > 0
                }
            )
    lifecycle_total = Counter(
        _text(_first(row, "action_type", "lifecycle", "lifecycle_status")) or "unobserved"
        for row in materialized
    )
    lifecycle_recovered = Counter(
        _text(_first(row, "action_type", "lifecycle", "lifecycle_status")) or "unobserved"
        for row in recovered
    )
    count_fields = {
        "predecessor_available": lambda row: _bool(row.get("predecessor_available")),
        "diff_available": lambda row: _bool(row.get("deterministic_diff_available")),
        "candidate_found": lambda row: bool(_first(row, "candidate_body", "method_b_candidate")),
        "b_safe": lambda row: _text(_first(row, "method_b_status", "status")) == "b_safe",
        "fallback_to_promote": lambda row: (
            _status(row) == "fallback"
            and _text(_first(row, "method_b_status", "status")) == "b_safe"
        ),
        "review_to_promote": lambda row: (
            _status(row) == "review" and _text(_first(row, "method_b_status", "status")) == "b_safe"
        ),
        "empty_target_recovered": lambda row: (
            not _text(_first(row, "target_text", "trusted_text", "anchor_text"))
            and _text(_first(row, "method_b_status", "status")) == "b_safe"
        ),
    }
    failures = Counter(
        reason
        for row in materialized
        for reason in _json_reasons(_first(row, "reason_codes_json", "method_b_reasons"))
    )
    return {
        "input_count": total,
        "recovered_count": len(recovered),
        "recovery_yield": len(recovered) / total if total else None,
        "pipeline_counts": {
            name: sum(predicate(row) for row in materialized)
            for name, predicate in count_fields.items()
        },
        "remaining": {
            "fallback": sum(
                _status(row) == "fallback"
                and _text(_first(row, "method_b_status", "status")) != "b_safe"
                for row in materialized
            ),
            "review": sum(
                _status(row) == "review"
                and _text(_first(row, "method_b_status", "status")) != "b_safe"
                for row in materialized
            ),
        },
        "failure_reasons": [
            {"reason": reason, "count": count} for reason, count in sorted(failures.items())
        ],
        "lifecycle": [
            {
                "lifecycle": key,
                "input_count": lifecycle_total[key],
                "recovered_count": lifecycle_recovered[key],
                "yield": lifecycle_recovered[key] / lifecycle_total[key],
            }
            for key in sorted(lifecycle_total)
        ],
        "recovered_markup_categories": [
            {"category": key, "count": categories[key]} for key in sorted(categories)
        ],
        "additional_markup_occurrences": [
            {"category": key, "count": additions[key]} for key in sorted(additions)
        ],
    }


def _json_reasons(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [_text(item) for item in value if _text(item)]
    try:
        parsed = json.loads(_text(value))
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [_text(item) for item in parsed if _text(item)]
    return [part for part in _text(value).split("|") if part]


def _excerpt(raw: Any, start: Any, end: Any, limit: int) -> str:
    text = _text(raw)
    try:
        left = max(0, int(start) - limit // 2)
        right = min(len(text), int(end) + limit // 2)
    except (TypeError, ValueError):
        left, right = 0, min(len(text), limit)
    return text[left:right]


def _range_excerpts(raw: Any, ranges: Any, limit: int) -> list[dict[str, Any]]:
    if isinstance(ranges, str):
        try:
            ranges = json.loads(ranges)
        except json.JSONDecodeError:
            ranges = []
    output = []
    for value in ranges if isinstance(ranges, list) else []:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        output.append(
            {
                "start": value[0],
                "end": value[1],
                "excerpt": _excerpt(raw, value[0], value[1], limit),
            }
        )
    return output


def blinded_audit_packet(
    rows: Iterable[Mapping[str, Any]],
    *,
    seed: int | str = 0,
    excerpt_limit: int = 500,
) -> dict[str, Any]:
    """Build a reviewer packet and a separate unblinding key/strata manifest."""
    packet: list[dict[str, Any]] = []
    key: list[dict[str, Any]] = []
    manifest: Counter[str] = Counter()
    for source in sorted((dict(row) for row in rows), key=_uid):
        uid, strata = _uid(source), _strata(source)
        for stratum in strata:
            manifest[stratum] += 1
        flip = int(_stable_hash(seed, uid, "label-order")[:2], 16) % 2
        method_1, method_2 = ("method_a", "method_b") if flip == 0 else ("method_b", "method_a")
        candidates = {
            "method_a": _text(
                _first(
                    source,
                    "method_a_candidate_raw_body",
                    "candidate_raw_body",
                    "method_a_candidate",
                )
            ),
            "method_b": _text(
                _first(
                    source,
                    "method_b_candidate_raw_body",
                    "control_candidate_raw_body",
                    "method_b_candidate",
                )
            ),
        }
        full_candidates = {
            "method_a": _text(source.get("method_a_candidate_full_raw")),
            "method_b": _text(_first(source, "candidate_raw", "method_b_candidate_full_raw")),
        }
        evidence_fields = (
            "revision_id",
            "predecessor_revision_id",
            "page_id",
            "action_type",
            "changed_ranges_json",
            "candidate_start",
            "candidate_end",
            "body_start",
            "body_end",
            "boundary_evidence_json",
            "signature_timestamp",
            "signature_author",
            "revision_actor",
            "wikiconv_speaker",
            "wikidisputes_speaker",
            "action_offset_hint",
            "lifecycle_consistency",
            "assignment_status",
            "assignment_evidence_json",
            "assignment_conflicts_json",
            "evidence_pointer",
        )
        packet.append(
            {
                "audit_uid": "revision-diff-audit:" + _stable_hash(seed, uid)[:20],
                "entity_uid": uid,
                "predecessor_excerpt": _excerpt(
                    _first(
                        source,
                        "predecessor_wikitext",
                        "predecessor_text",
                        "predecessor_raw_body",
                    ),
                    _first(source, "predecessor_changed_start", "predecessor_start"),
                    _first(source, "predecessor_changed_end", "predecessor_end"),
                    excerpt_limit,
                ),
                "target_excerpt": _excerpt(
                    _first(
                        source,
                        "target_wikitext",
                        "target_text",
                        "trusted_text",
                        "anchor_text",
                    ),
                    _first(source, "target_changed_start", "target_start"),
                    _first(source, "target_changed_end", "target_end"),
                    excerpt_limit,
                ),
                "predecessor_changed_excerpts": _range_excerpts(
                    _first(source, "predecessor_wikitext", "predecessor_text"),
                    source.get("predecessor_changed_ranges_json"),
                    excerpt_limit,
                ),
                "target_changed_excerpts": _range_excerpts(
                    _first(source, "target_wikitext", "target_text"),
                    source.get("target_changed_ranges_json"),
                    excerpt_limit,
                ),
                "candidate_1_label": "Candidate 1",
                "candidate_1_raw_body": candidates[method_1],
                "candidate_1_full_raw": full_candidates[method_1],
                "candidate_2_label": "Candidate 2",
                "candidate_2_raw_body": candidates[method_2],
                "candidate_2_full_raw": full_candidates[method_2],
                "evidence": {
                    field: source.get(field)
                    for field in evidence_fields
                    if source.get(field) not in (None, "")
                },
                "review_decision": None,
                "review_notes": None,
            }
        )
        key.append(
            {
                "audit_uid": packet[-1]["audit_uid"],
                "entity_uid": uid,
                "candidate_1_method": method_1,
                "candidate_2_method": method_2,
                "method_a_status": _status(source),
                "method_b_status": _text(_first(source, "method_b_status", "status")),
                "strata": strata,
            }
        )
    return {
        "seed": seed,
        "reviewer_rows": packet,
        "unblinding_key": key,
        "strata_manifest": [
            {"stratum": name, "population_count": manifest[name]} for name in sorted(manifest)
        ],
    }


# Descriptive aliases make the module convenient for orchestration code.
build_profile_report = profile_rows
build_pilot_validation_report = pilot_validation_report
build_recovery_report = recovery_report
build_blinded_audit_packet = blinded_audit_packet
