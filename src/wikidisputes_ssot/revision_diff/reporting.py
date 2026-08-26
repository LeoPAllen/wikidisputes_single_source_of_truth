"""Pure, deterministic reporting helpers for revision-diff recovery work.

The helpers deliberately accept ordinary mappings.  They are intended to be
called by a storage/CLI layer, but neither read nor write project artifacts.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
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
    if _bool(_first(row, "multi_action_revision", "revision_is_multi_action")) or int(
        _first(row, "action_count_in_revision", "revision_action_count") or 1
    ) > 1:
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
                "lifecycle": _text(
                    _first(row, "action_type", "lifecycle", "lifecycle_status")
                )
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
                "predecessor_availability": _text(
                    row.get("predecessor_availability")
                )
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
    return sorted({
        token
        for token in tokens
        if token in negations
        or token in operators
        or token.startswith(("http://", "https://"))
        or any(char.isdigit() for char in token)
    })


def _comparison(left: Any, right: Any) -> dict[str, Any]:
    raw_left, raw_right = _text(left), _text(right)
    visible_left, visible_right = visible_text(raw_left), visible_text(raw_right)
    left_tokens, right_tokens = comparison_tokens(raw_left), comparison_tokens(raw_right)
    return {
        "raw_equal": raw_left == raw_right,
        "visible_equal": visible_left == visible_right,
        "raw_left": raw_left,
        "raw_right": raw_right,
        "visible_left": visible_left,
        "visible_right": visible_right,
        "left_tokens": left_tokens,
        "right_tokens": right_tokens,
        "missing_critical_tokens": _critical_tokens(set(left_tokens) - set(right_tokens)),
        "added_critical_tokens": _critical_tokens(set(right_tokens) - set(left_tokens)),
    }


def pilot_validation_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Describe A/B agreement without treating Method A as truth."""
    out: list[dict[str, Any]] = []
    for row in rows:
        source = dict(row)
        a = _first(
            source, "method_a_candidate_raw_body", "candidate_raw_body", "method_a_candidate"
        )
        b = _first(
            source,
            "method_b_candidate_raw_body",
            "control_candidate_raw_body",
            "method_b_candidate",
        )
        comparison = _comparison(a, b)
        left_a = _first(source, "method_a_left_boundary", "left_boundary")
        left_b = _first(source, "method_b_left_boundary", "control_left_boundary")
        right_a = _first(source, "method_a_right_boundary", "right_boundary")
        right_b = _first(source, "method_b_right_boundary", "control_right_boundary")
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
                "critical_token_difference": bool(
                    comparison["missing_critical_tokens"]
                    or comparison["added_critical_tokens"]
                ),
                "left_boundaries": {"method_a": left_a, "method_b": left_b},
                "right_boundaries": {"method_a": right_a, "method_b": right_b},
                "left_boundary_equal": left_a == left_b,
                "right_boundary_equal": right_a == right_b,
                "contamination_agree": _first(
                    source, "method_a_contamination", "contamination"
                )
                == _first(source, "method_b_contamination", "control_contamination"),
                "assignment_ambiguity_agree": _first(
                    source, "method_a_assignment_ambiguity", "assignment_ambiguity"
                )
                == _first(
                    source,
                    "method_b_assignment_ambiguity",
                    "control_assignment_ambiguity",
                ),
                "method_b_adjacent_contamination": _bool(
                    _first(
                        source,
                        "method_b_contamination",
                        "neighboring_comment_contamination",
                    )
                ),
                "method_b_assignment_ambiguous": _bool(
                    _first(
                        source,
                        "method_b_assignment_ambiguity",
                        "assignment_ambiguity",
                    )
                ),
                "lifecycle": _text(
                    _first(source, "action_type", "lifecycle", "lifecycle_status")
                )
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
                "raw_body_agreement_yield": sum(
                    item["candidate"]["raw_equal"] for item in members
                )
                / len(members),
                "visible_text_agreement_yield": sum(
                    item["candidate"]["visible_equal"] for item in members
                )
                / len(members),
                "left_boundary_agreement_yield": sum(
                    item["left_boundary_equal"] for item in members
                )
                / len(members),
                "right_boundary_agreement_yield": sum(
                    item["right_boundary_equal"] for item in members
                )
                / len(members),
                "critical_token_difference_count": sum(
                    item["critical_token_difference"] for item in members
                ),
                "adjacent_contamination_count": sum(
                    item["method_b_adjacent_contamination"] for item in members
                ),
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
            "exact_raw_body_agreement_count": sum(
                row["candidate"]["raw_equal"] for row in comparisons
            ),
            "visible_text_agreement_count": sum(
                row["candidate"]["visible_equal"] for row in comparisons
            ),
            "left_boundary_agreement_count": sum(
                row["left_boundary_equal"] for row in comparisons
            ),
            "right_boundary_agreement_count": sum(
                row["right_boundary_equal"] for row in comparisons
            ),
            "critical_token_difference_count": sum(
                row["critical_token_difference"] for row in comparisons
            ),
            "adjacent_contamination_count": sum(
                row["method_b_adjacent_contamination"] for row in comparisons
            ),
            "assignment_ambiguity_count": sum(
                row["method_b_assignment_ambiguous"] for row in comparisons
            ),
        },
        "method_a_safe_controls": {
            "comparison_count": len(controls),
            "exact_raw_body_agreement_count": sum(
                row["candidate"]["raw_equal"] for row in controls
            ),
            "visible_text_agreement_count": sum(
                row["candidate"]["visible_equal"] for row in controls
            ),
        },
        "status_pairs": [
            {"method_a_status": pair[0], "method_b_status": pair[1], "count": count}
            for pair, count in sorted(
                Counter(
                    (row["method_a_status"], row["method_b_status"])
                    for row in comparisons
                ).items()
            )
        ],
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
        _text(_first(row, "action_type", "lifecycle", "lifecycle_status"))
        or "unobserved"
        for row in materialized
    )
    lifecycle_recovered = Counter(
        _text(_first(row, "action_type", "lifecycle", "lifecycle_status"))
        or "unobserved"
        for row in recovered
    )
    count_fields = {
        "predecessor_available": lambda row: _bool(row.get("predecessor_available")),
        "diff_available": lambda row: _bool(row.get("deterministic_diff_available")),
        "candidate_found": lambda row: bool(_first(row, "candidate_body", "method_b_candidate")),
        "b_safe": lambda row: _text(_first(row, "method_b_status", "status")) == "b_safe",
        "fallback_to_promote": lambda row: _status(row) == "fallback"
        and _text(_first(row, "method_b_status", "status")) == "b_safe",
        "review_to_promote": lambda row: _status(row) == "review"
        and _text(_first(row, "method_b_status", "status")) == "b_safe",
        "empty_target_recovered": lambda row: not _text(
            _first(row, "target_text", "trusted_text", "anchor_text")
        )
        and _text(_first(row, "method_b_status", "status")) == "b_safe",
    }
    failures = Counter(
        reason
        for row in materialized
        for reason in _json_reasons(
            _first(row, "reason_codes_json", "method_b_reasons")
        )
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
            {"reason": reason, "count": count}
            for reason, count in sorted(failures.items())
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
        method_1, method_2 = (
            ("method_a", "method_b") if flip == 0 else ("method_b", "method_a")
        )
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
            "method_b": _text(
                _first(source, "candidate_raw", "method_b_candidate_full_raw")
            ),
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
                "method_b_status": _text(
                    _first(source, "method_b_status", "status")
                ),
                "strata": strata,
            }
        )
    return {
        "seed": seed,
        "reviewer_rows": packet,
        "unblinding_key": key,
        "strata_manifest": [
            {"stratum": name, "population_count": manifest[name]}
            for name in sorted(manifest)
        ],
    }


# Descriptive aliases make the module convenient for orchestration code.
build_profile_report = profile_rows
build_pilot_validation_report = pilot_validation_report
build_recovery_report = recovery_report
build_blinded_audit_packet = blinded_audit_packet
