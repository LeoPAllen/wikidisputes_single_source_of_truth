"""Fail-closed, read-only probes for residual revision-diff rows.

These rules deliberately return audit suggestions only; nothing in this module
participates in recovery or selection.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from itertools import combinations
from typing import Any

from .boundaries import extract_comment_candidates

RULE_FAMILIES = ("X1", "R1", "C1a", "M1", "B1")
_TRIVIAL_OUTER_RE = re.compile(
    r"(?:\s|<!--.*?-->|</?(?:small|span|sup|sub)\b[^>]*>|&nbsp;)+", re.I | re.S
)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _value(row: Mapping[str, Any], name: str) -> Any:
    """Read both compact rows and the bundle's prefixed evidence columns."""
    if row.get(name) not in (None, ""):
        return row[name]
    for prefix in ("recovery__", "source__", "selection__"):
        key = prefix + name
        if key in row:
            return row[key]
    return None


def _hard_left_boundary(raw: str, start: int) -> bool:
    if start == 0:
        return True
    prefix = raw[:start]
    trimmed = prefix.rstrip()
    if not trimmed:
        return True
    line = trimmed.rsplit("\n", 1)[-1]
    if bool(re.fullmatch(r"\s*=+[^=\n].*?=+\s*", line) or re.match(r"\s*\{\{", line)):
        return True
    candidates = extract_comment_candidates(trimmed)
    return bool(candidates and candidates[-1].end == len(trimmed))


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _spans(row: Mapping[str, Any], name: str) -> list[tuple[int, int]]:
    return [
        (item[0], item[1])
        for item in _list(_value(row, name))
        if isinstance(item, (list, tuple))
        and len(item) == 2
        and isinstance(item[0], int)
        and isinstance(item[1], int)
        and item[0] < item[1]
    ]


def _candidates(row: Mapping[str, Any], raw: str) -> list[dict[str, Any]]:
    supplied = row.get("all_candidates")
    if isinstance(supplied, list):
        output = [item for item in supplied if isinstance(item, dict)]
        if output:
            return output
    return [asdict(item) for item in extract_comment_candidates(raw)]


def _matches(raw: str, source: str) -> list[tuple[int, int]]:
    return (
        [(match.start(), match.end()) for match in re.finditer(re.escape(source), raw)]
        if source
        else []
    )


def _contains(candidate: Mapping[str, Any], span: tuple[int, int]) -> bool:
    return (
        isinstance(candidate.get("start"), int)
        and isinstance(candidate.get("end"), int)
        and candidate["start"] <= span[0]
        and span[1] <= candidate["end"]
    )


def _speaker_matches(row: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    speaker = _text(_value(row, "wikiconv_speaker")).strip().replace("_", " ").casefold()
    author = _text(candidate.get("signature_user_target")).strip().replace("_", " ").casefold()
    return bool(not speaker or (author and author == speaker))


def _safe(row: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    contamination = _text(_value(row, "neighboring_comment_contamination")).casefold()
    if contamination and contamination != "clean":
        return False
    if candidate.get("boundary_warnings"):
        return False
    if _list(_value(row, "critical_token_contradictions_json")):
        return False
    return not any(
        "contamination" in str(item).casefold()
        for item in _list(_value(row, "ambiguity_flags_json"))
    )


def _conflict_free(row: Mapping[str, Any]) -> bool:
    return (
        not _list(_value(row, "competing_candidates_json"))
        and not _list(_value(row, "competing_actions_json"))
        and not row.get("competing_candidate_evidence")
        and not row.get("competing_action_evidence")
    )


def _result(
    row: Mapping[str, Any],
    family: str,
    *,
    candidate: Mapping[str, Any] | None = None,
    eligible: bool = False,
    evidence: str = "",
    blocker: str | None = None,
) -> dict[str, Any]:
    return {
        "source_row_uid": _value(row, "source_row_uid"),
        "rule_family": family,
        "eligible": eligible,
        "proposed_action_uid": _value(row, "action_uid"),
        "proposed_candidate_uid": candidate.get("candidate_uid") if candidate else None,
        "raw_bounds": [candidate["start"], candidate["end"]]
        if candidate
        and isinstance(candidate.get("start"), int)
        and isinstance(candidate.get("end"), int)
        else None,
        "body_bounds": [candidate["body_start"], candidate["body_end"]]
        if candidate
        and isinstance(candidate.get("body_start"), int)
        and isinstance(candidate.get("body_end"), int)
        else None,
        "evidence": evidence or None,
        "blocker": blocker,
        "status": _value(row, "status") or row.get("current_status"),
        "lifecycle": _value(row, "action_type") or row.get("lifecycle"),
        "primary_stratum": row.get("primary_stratum"),
        "inclusion_probability": row.get("inclusion_probability"),
        "survey_weight": row.get("survey_weight") or row.get("sample_weight"),
        "review_order": row.get("review_order"),
    }


def _x1_candidate(row: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    raw, source = _text(row.get("target_wikitext")), _text(_value(row, "source_text"))
    matches = _matches(raw, source)
    if not source or len(matches) != 1:
        return None, "source_not_unique_in_target"
    containing = [
        candidate for candidate in _candidates(row, raw) if _contains(candidate, matches[0])
    ]
    if len(containing) != 1:
        return None, "not_exactly_one_containing_candidate"
    candidate = containing[0]
    if _text(candidate.get("body_wikitext")).strip() != source.strip():
        return None, "candidate_body_not_exact_source"
    if _text(_value(row, "lifecycle_consistency")) not in {
        "target_change_localized",
    }:
        return None, "lifecycle_not_compatible"
    if not _speaker_matches(row, candidate):
        return None, "signature_speaker_mismatch"
    if not _safe(row, candidate):
        return None, "contamination_or_boundary_safety"
    if not _conflict_free(row):
        return None, "competing_candidate_or_action"
    return candidate, None


def probe_x1(row: Mapping[str, Any]) -> dict[str, Any]:
    candidate, blocker = _x1_candidate(row)
    return _result(
        row,
        "X1",
        candidate=candidate,
        eligible=candidate is not None,
        evidence="unique_exact_source_existing_candidate" if candidate else "",
        blocker=blocker,
    )


def probe_r1(row: Mapping[str, Any]) -> dict[str, Any]:
    candidate, blocker = _x1_candidate(row)
    reasons = _list(_value(row, "reason_codes_json")) + _list(
        _value(row, "assignment_reason_codes_json")
    )
    limited = any(
        str(reason)
        in {
            "revision_too_large_for_safe_global_assignment",
            "global_assignment_search_limit",
        }
        for reason in reasons
    )
    if candidate is None:
        return _result(row, "R1", blocker=blocker)
    if not limited:
        return _result(row, "R1", blocker="not_resource_limited")
    if _text(_value(row, "action_type") or row.get("lifecycle")) == "restoration":
        return _result(row, "R1", blocker="restoration_excluded")
    return _result(
        row,
        "R1",
        candidate=candidate,
        eligible=True,
        evidence="x1_proof_bypasses_resource_limit_only",
    )


def probe_c1a(row: Mapping[str, Any]) -> dict[str, Any]:
    if _text(_value(row, "action_type") or row.get("lifecycle")) != "creation":
        return _result(row, "C1a", blocker="not_creation")
    raw, source = _text(row.get("target_wikitext")), _text(_value(row, "source_text"))
    matches = _matches(raw, source)
    if not source or len(matches) != 1:
        return _result(row, "C1a", blocker="source_not_unique_in_target")
    if _value(row, "action_count") not in (1, "1"):
        return _result(row, "C1a", blocker="not_unique_action")
    offset = matches[0][0]
    if any(_contains(item, matches[0]) for item in _candidates(row, raw)):
        return _result(row, "C1a", blocker="existing_candidate_already_contains_source")
    candidates = []
    for item in extract_comment_candidates(raw[offset:]):
        candidate = asdict(item)
        for key in ("start", "end", "body_start", "body_end", "signature_start", "signature_end"):
            if candidate[key] is not None:
                candidate[key] += offset
        candidates.append(candidate)
    found = [
        item
        for item in candidates
        if item["start"] == offset and item["body_wikitext"].strip() == source.strip()
    ]
    if len(found) != 1:
        return _result(row, "C1a", blocker="no_unique_signed_candidate_at_source")
    candidate = found[0]
    if (
        candidate["boundary_warnings"]
        or not candidate["boundary_evidence"]
        or not _hard_left_boundary(raw, offset)
        or not _speaker_matches(row, candidate)
    ):
        return _result(row, "C1a", blocker="signature_or_left_boundary_not_defensible")
    changed = _spans(row, "action_target_changed_ranges_json") or _spans(
        row, "target_changed_ranges_json"
    )
    if not changed or not all(
        candidate["start"] <= start and end <= candidate["end"] for start, end in changed
    ):
        return _result(row, "C1a", blocker="changed_spans_not_closed")
    if not _conflict_free(row) or candidate["boundary_warnings"]:
        return _result(row, "C1a", blocker="competing_or_unsafe_boundary")
    return _result(
        row,
        "C1a",
        candidate=candidate,
        eligible=True,
        evidence="unique_changed_span_closed_signed_creation",
    )


def probe_m1(row: Mapping[str, Any]) -> dict[str, Any]:
    if _text(_value(row, "action_type") or row.get("lifecycle")) != "modification":
        return _result(row, "M1", blocker="not_modification")
    source, before, after = (
        _text(_value(row, "source_text")),
        _text(row.get("predecessor_wikitext")),
        _text(row.get("target_wikitext")),
    )
    if not source or len(_matches(before, source)) != 1 or len(_matches(after, source)) != 1:
        return _result(row, "M1", blocker="source_not_unique_in_predecessor_and_target")
    start = _matches(after, source)[0][0]
    found = [
        asdict(item)
        for item in extract_comment_candidates(after)
        if item.start == start and item.body_wikitext.strip() == source.strip()
    ]
    if len(found) != 1 or not _speaker_matches(row, found[0]):
        return _result(row, "M1", blocker="no_unique_matching_autosign_candidate")
    candidate = found[0]
    if _value(row, "action_count") not in (1, "1"):
        return _result(row, "M1", blocker="not_unique_action")
    if not _conflict_free(row) or not _safe(row, candidate):
        return _result(row, "M1", blocker="conflicted_or_unsafe")
    changed = _spans(row, "action_target_changed_ranges_json") or _spans(
        row, "target_changed_ranges_json"
    )
    if not changed or not all(
        candidate["signature_start"] <= start and end <= candidate["end"] for start, end in changed
    ):
        return _result(row, "M1", blocker="target_change_not_signature_confined")
    # Removing the signature and harmless surrounding material must restore the predecessor exactly.
    stripped = after[: candidate["signature_start"]] + after[candidate["signature_end"] :]
    residual = stripped[len(before) :] if stripped.startswith(before) else None
    if residual is None or (residual and not _TRIVIAL_OUTER_RE.fullmatch(residual)):
        return _result(row, "M1", blocker="change_not_signature_only")
    return _result(
        row,
        "M1",
        candidate=candidate,
        eligible=True,
        evidence="unique_body_preserved_signature_added",
    )


def probe_b1(row: Mapping[str, Any]) -> dict[str, Any]:
    raw, source = _text(row.get("target_wikitext")), _text(_value(row, "source_text"))
    matches = _matches(raw, source)
    if not source or len(matches) != 1:
        return _result(row, "B1", blocker="source_not_unique_in_target")
    containing = [item for item in _candidates(row, raw) if _contains(item, matches[0])]
    if len(containing) != 1:
        return _result(row, "B1", blocker="not_exactly_one_existing_candidate")
    candidate = containing[0]
    body = _text(candidate.get("body_wikitext"))
    body_matches = _matches(body, source)
    if len(body_matches) != 1:
        return _result(row, "B1", blocker="source_not_unique_in_candidate_body")
    left, right = body[: body_matches[0][0]], body[body_matches[0][1] :]
    if (
        len(left) + len(right) > 64
        or (left and not _TRIVIAL_OUTER_RE.fullmatch(left))
        or (right and not _TRIVIAL_OUTER_RE.fullmatch(right))
    ):
        return _result(row, "B1", blocker="body_discrepancy_is_substantive")
    if not (left or right) or not _safe(row, candidate) or not _conflict_free(row):
        return _result(row, "B1", blocker="no_tiny_safe_repair")
    if _text(
        _value(row, "lifecycle_consistency")
    ) != "target_change_localized" or not _speaker_matches(row, candidate):
        return _result(row, "B1", blocker="lifecycle_or_signature_not_corroborated")
    repaired = dict(candidate)
    repaired["body_start"], repaired["body_end"] = matches[0]
    return _result(
        row,
        "B1",
        candidate=repaired,
        eligible=True,
        evidence="allowlisted_tiny_outer_boundary_repair",
    )


def run_probes(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    probes = (probe_x1, probe_r1, probe_c1a, probe_m1, probe_b1)
    return [result for row in rows for probe in probes for result in (probe(row),)]


def summarize_probe_results(
    rows: Iterable[Mapping[str, Any]], results: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    rows, results = list(rows), list(results)
    weights = {
        str(_value(row, "source_row_uid")): float(
            row.get("survey_weight") or row.get("sample_weight") or 1
        )
        for row in rows
    }
    eligible: dict[str, set[str]] = defaultdict(set)
    breakdown: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    blockers: dict[str, Counter[str]] = defaultdict(Counter)
    by_uid = {str(_value(row, "source_row_uid")): row for row in rows}
    for result in results:
        family = str(result.get("rule_family"))
        if result.get("blocker"):
            blockers[family][str(result["blocker"])] += 1
        if result.get("eligible"):
            uid = str(result.get("source_row_uid"))
            eligible[family].add(uid)
            row = by_uid.get(uid, {})
            status = _value(row, "status") or row.get("current_status") or "unknown"
            lifecycle = _value(row, "action_type") or row.get("lifecycle") or "unknown"
            breakdown[family][f"{status}|{lifecycle}"].add(uid)
    any_rule = set().union(*(eligible[family] for family in RULE_FAMILIES))
    return {
        "rule_families": {
            family: {
                "sample_hits": len(eligible[family]),
                "weighted_residual_rows": sum(weights.get(uid, 0) for uid in eligible[family]),
                "status_lifecycle": {
                    cell: {
                        "sample_hits": len(cell_uids),
                        "weighted_residual_rows": sum(weights.get(uid, 0) for uid in cell_uids),
                    }
                    for cell, cell_uids in sorted(breakdown[family].items())
                },
                "blocker_counts": dict(sorted(blockers[family].items())),
            }
            for family in RULE_FAMILIES
        },
        "overlaps": {
            f"{left}|{right}": {
                "sample_hits": len(eligible[left] & eligible[right]),
                "weighted_residual_rows": sum(
                    weights.get(uid, 0) for uid in eligible[left] & eligible[right]
                ),
            }
            for left, right in combinations(RULE_FAMILIES, 2)
        },
        "any_rule": {
            "sample_hits": len(any_rule),
            "weighted_residual_rows": sum(weights.get(uid, 0) for uid in any_rule),
        },
    }
