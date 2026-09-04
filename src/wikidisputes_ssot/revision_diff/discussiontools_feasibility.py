"""Pure, deliberately conservative primitives for a DiscussionTools pilot.

This module neither renders a revision nor discovers raw comments.  It makes
the sampling, rendered-to-raw decision, and feasibility gate reproducible so a
runner can persist its inputs and results independently of this policy.
"""

from __future__ import annotations

import hashlib
import html
import json
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from wikidisputes_ssot.promotion_safety import visible_text

from .boundaries import BoundaryCandidate

PROVENANCE_TAG = "rendered_structure_discussiontools"
ContaminationStatus = Literal["clean", "detected", "unknown"]


@dataclass(frozen=True)
class Stratum:
    name: str
    size: int


STRATA: tuple[Stratum, ...] = (
    Stratum("control_method_a_promote", 20),
    Stratum("control_method_b_safe_usable", 20),
    Stratum("restoration", 10),
    Stratum("modification", 15),
    Stratum("creation", 15),
    Stratum("unsigned_malformed", 20),
    Stratum("prior_diff_span_structural", 20),
    Stratum("multi_action", 20),
    Stratum("b_review", 25),
    Stratum("b_no_candidate", 35),
)


@dataclass(frozen=True)
class FeasibilitySample:
    """A selected joined row. ``row`` is intentionally retained losslessly."""

    source_row_uid: str
    stratum: str
    priority: int
    matching_labels: tuple[str, ...]
    row: Mapping[str, Any]


@dataclass(frozen=True)
class SampleSelection:
    samples: tuple[FeasibilitySample, ...]
    requested: Mapping[str, int]
    selected: Mapping[str, int]
    shortfalls: Mapping[str, int]

    @property
    def complete(self) -> bool:
        return not self.shortfalls and len(self.samples) == 200


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _status(row: Mapping[str, Any]) -> str:
    return _text(row.get("method_b_status", row.get("status"))).casefold()


def _lifecycle(row: Mapping[str, Any]) -> str:
    value = row.get("action_type", row.get("lifecycle", row.get("lifecycle_status")))
    return _text(value).casefold()


def _jsonish_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.casefold(),)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(_text(item).casefold() for item in value)
    return ()


def _contains(row: Mapping[str, Any], *needles: str) -> bool:
    values: list[str] = []
    for key in (
        "boundary_method",
        "boundary_evidence",
        "boundary_evidence_json",
        "boundary_warnings",
        "boundary_warnings_json",
        "reason_codes",
        "reason_codes_json",
        "signature_status",
        "structural_warnings_json",
    ):
        values.extend(_jsonish_values(row.get(key)))
    haystack = " ".join(values)
    return any(needle.casefold() in haystack for needle in needles)


def _is_a_promote(row: Mapping[str, Any]) -> bool:
    value = row.get("method_a_status", row.get("method_a_decision", row.get("decision")))
    return _text(value).casefold() == "promote"


def _is_a_control(row: Mapping[str, Any]) -> bool:
    """A control must also carry independent action spans for this mapper."""

    spans = row.get("action_target_changed_ranges_json")
    if isinstance(spans, str):
        try:
            spans = json.loads(spans)
        except json.JSONDecodeError:
            spans = None
    return (
        _is_a_promote(row)
        and row.get("method_a_left_boundary") is not None
        and row.get("method_a_right_boundary") is not None
        and isinstance(row.get("method_a_candidate_full_raw"), str)
        and isinstance(spans, Sequence)
        and not isinstance(spans, (str, bytes, bytearray))
        and bool(spans)
    )


def _is_b_control(row: Mapping[str, Any]) -> bool:
    return _status(row) in {"b_safe", "b_usable"}


def _is_unsigned_or_malformed(row: Mapping[str, Any]) -> bool:
    signature = _text(row.get("signature_status")).casefold()
    return signature in {"unsigned", "malformed", "missing"} or _contains(
        row, "unsigned", "malformed"
    )


def _stable_order(row: Mapping[str, Any], seed: str) -> tuple[str, str]:
    uid = _text(row.get("source_row_uid"))
    digest = hashlib.sha256(f"{seed}\0{uid}".encode()).hexdigest()
    return digest, uid


def _action_count(row: Mapping[str, Any]) -> int:
    """Read either revision-level action count spelling without guessing."""

    value = row.get("action_count_in_revision", row.get("action_count", 0))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _matching_labels(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Retain overlapping eligibility labels even though selection is disjoint."""

    labels: list[str] = []
    if _is_a_promote(row):
        labels.append("control_method_a_promote")
    if not _is_a_promote(row) and _is_b_control(row):
        labels.append("control_method_b_safe_usable")
    if _is_a_promote(row) or _is_b_control(row):
        return tuple(labels)
    if _lifecycle(row) == "restoration":
        labels.append("restoration")
    if _lifecycle(row) == "modification":
        labels.append("modification")
    if _lifecycle(row) == "creation":
        labels.append("creation")
    if _is_unsigned_or_malformed(row):
        labels.append("unsigned_malformed")
    if _contains(row, "diff_span_structural"):
        labels.append("prior_diff_span_structural")
    if _action_count(row) > 1:
        labels.append("multi_action")
    if _status(row) == "b_review":
        labels.append("b_review")
    if _status(row) == "b_no_candidate":
        labels.append("b_no_candidate")
    return tuple(labels)


def select_feasibility_sample(rows: Sequence[Mapping[str, Any]], *, seed: str) -> SampleSelection:
    """Select the fixed 200-row pilot with disjoint, priority-ordered strata.

    Missing rows are reported as shortfalls.  A duplicated or absent source UID
    is rejected because stable resumability depends on that identity.
    """

    by_uid: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        uid = _text(row.get("source_row_uid"))
        if not uid or uid in by_uid:
            raise ValueError("feasibility rows require unique source_row_uid values")
        by_uid[uid] = row

    picked: set[str] = set()
    output: list[FeasibilitySample] = []
    selected: dict[str, int] = {}
    requested = {spec.name: spec.size for spec in STRATA}

    def choose(spec: Stratum, predicate: Callable[[Mapping[str, Any]], bool]) -> None:
        candidates = [row for uid, row in by_uid.items() if uid not in picked and predicate(row)]
        candidates.sort(key=lambda row: _stable_order(row, seed))
        members = candidates[: spec.size]
        for row in members:
            uid = _text(row.get("source_row_uid"))
            picked.add(uid)
            output.append(
                FeasibilitySample(uid, spec.name, len(output), _matching_labels(row), row)
            )
        selected[spec.name] = len(members)

    choose(STRATA[0], _is_a_control)
    choose(STRATA[1], lambda row: not _is_a_promote(row) and _is_b_control(row))

    # Everything following the controls is the unresolved residual population.
    def residual(row: Mapping[str, Any]) -> bool:
        return not _is_a_promote(row) and not _is_b_control(row)

    choose(STRATA[2], lambda row: residual(row) and _lifecycle(row) == "restoration")
    choose(STRATA[3], lambda row: residual(row) and _lifecycle(row) == "modification")
    choose(STRATA[4], lambda row: residual(row) and _lifecycle(row) == "creation")
    choose(STRATA[5], lambda row: residual(row) and _is_unsigned_or_malformed(row))
    choose(STRATA[6], lambda row: residual(row) and _contains(row, "diff_span_structural"))
    choose(
        STRATA[7],
        lambda row: residual(row) and _action_count(row) > 1,
    )
    choose(STRATA[8], lambda row: residual(row) and _status(row) == "b_review")
    choose(STRATA[9], lambda row: residual(row) and _status(row) == "b_no_candidate")

    shortfalls = {
        spec.name: spec.size - selected[spec.name]
        for spec in STRATA
        if selected[spec.name] < spec.size
    }
    return SampleSelection(tuple(output), requested, selected, shortfalls)


def normalise_visible_text(value: str) -> str:
    """The only normalisation permitted for rendered-text anchor comparison."""

    return " ".join(unicodedata.normalize("NFC", html.unescape(_text(value))).split())


@dataclass(frozen=True)
class RenderedComment:
    visible_text: str
    author: str | None = None
    timestamp: str | None = None
    dom_anchor: str | None = None


@dataclass(frozen=True)
class RenderedMappingEvidence:
    target_spans: tuple[tuple[int, int], ...] = ()
    action_count: int = 1
    competing_candidate_count: int = 0
    competing_action_count: int = 0
    raw_boundaries_defensible: bool = True
    lifecycle_consistent: bool = True
    contamination_status: ContaminationStatus = "unknown"

    def __post_init__(self) -> None:
        if self.contamination_status not in {"clean", "detected", "unknown"}:
            raise ValueError("contamination_status must be clean, detected, or unknown")

    @property
    def contamination_detected(self) -> bool:
        """Compatibility-friendly shorthand for audit consumers."""

        return self.contamination_status == "detected"


@dataclass(frozen=True)
class RenderedMappingDecision:
    safe: bool
    matched_candidate: BoundaryCandidate | None
    provenance_tag: str | None
    failure_reasons: tuple[str, ...]
    matched_pair_count: int

    @property
    def candidate(self) -> BoundaryCandidate | None:
        """Evidentiary match; callers must check ``safe`` before promotion."""

        return self.matched_candidate


def _same_if_present(left: str | None, right: str | None) -> bool:
    """Require a parser-provided anchor to equal the raw anchor exactly."""

    if not left:
        return True
    return left == right


def evaluate_rendered_mapping(
    rendered_comments: Sequence[RenderedComment],
    raw_candidates: Sequence[BoundaryCandidate],
    evidence: RenderedMappingEvidence | None = None,
) -> RenderedMappingDecision:
    """Map a parsed comment only to a pre-existing, exactly bounded candidate.

    The deterministic reason order is part of the audit contract.  A parser
    result can never manufacture a raw candidate or relax Method-B boundaries.
    """

    evidence = evidence or RenderedMappingEvidence()
    pairs: list[tuple[RenderedComment, BoundaryCandidate]] = []
    for comment in rendered_comments:
        anchor = normalise_visible_text(comment.visible_text)
        if not anchor:
            continue
        for candidate in raw_candidates:
            body = normalise_visible_text(visible_text(candidate.body_wikitext))
            if body == anchor:
                pairs.append((comment, candidate))

    reasons: list[str] = []
    if len(rendered_comments) != 1:
        reasons.append("rendered_comment_not_unique")
    if len(raw_candidates) != 1:
        reasons.append("raw_candidate_not_unique")
    if len(pairs) != 1:
        reasons.append("visible_text_pair_not_unique")

    matched_candidate: BoundaryCandidate | None = pairs[0][1] if len(pairs) == 1 else None
    matched_comment: RenderedComment | None = pairs[0][0] if len(pairs) == 1 else None
    if matched_candidate is not None and matched_comment is not None:
        if not _same_if_present(matched_comment.author, matched_candidate.signature_user_target):
            reasons.append("author_mismatch")
        if not _same_if_present(matched_comment.timestamp, matched_candidate.signature_timestamp):
            reasons.append("timestamp_mismatch")
        if not evidence.raw_boundaries_defensible or not matched_candidate.boundary_evidence:
            reasons.append("raw_boundaries_not_defensible")
        if matched_candidate.boundary_warnings:
            reasons.append("raw_boundary_warnings_present")
        if not evidence.target_spans:
            reasons.append("action_spans_missing")
        elif any(
            start >= end or start < matched_candidate.start or end > matched_candidate.end
            for start, end in evidence.target_spans
        ):
            reasons.append("action_span_outside_candidate")
    else:
        # Do not imply that a candidate passed any candidate-specific predicate.
        if not evidence.target_spans:
            reasons.append("action_spans_missing")

    if evidence.action_count != 1:
        reasons.append("action_not_unique")
    if evidence.competing_candidate_count:
        reasons.append("competing_raw_candidate")
    if evidence.competing_action_count:
        reasons.append("competing_action")
    if not evidence.lifecycle_consistent:
        reasons.append("lifecycle_inconsistent")
    if evidence.contamination_status == "detected":
        reasons.append("contamination_detected")
    elif evidence.contamination_status == "unknown":
        reasons.append("contamination_unknown")

    safe = not reasons and matched_candidate is not None
    return RenderedMappingDecision(
        safe=safe,
        matched_candidate=matched_candidate,
        provenance_tag=PROVENANCE_TAG if safe else None,
        failure_reasons=tuple(reasons),
        matched_pair_count=len(pairs),
    )


@dataclass(frozen=True)
class FeasibilityResult:
    source_row_uid: str
    stratum: str
    is_control: bool
    parser_success: bool
    exact_boundary_agreement: bool = False
    contamination_status: ContaminationStatus = "unknown"
    proposed_safe: bool = False
    b_status: str = ""
    lifecycle: str = ""
    failure_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.contamination_status not in {"clean", "detected", "unknown"}:
            raise ValueError("contamination_status must be clean, detected, or unknown")

    @property
    def contamination_detected(self) -> bool:
        return self.contamination_status == "detected"

    @property
    def contamination_unknown(self) -> bool:
        return self.contamination_status == "unknown"


@dataclass(frozen=True)
class FeasibilityGate:
    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FeasibilityReport:
    overall: Mapping[str, float | int]
    controls: Mapping[str, Any]
    residual: Mapping[str, Any]
    parser_subgroups: Mapping[str, Mapping[str, float | int]]
    gate: FeasibilityGate


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _contamination_counts(results: Sequence[FeasibilityResult]) -> dict[str, int]:
    counts = Counter(result.contamination_status for result in results)
    statuses: tuple[ContaminationStatus, ...] = ("clean", "detected", "unknown")
    return {status: counts[status] for status in statuses}


def feasibility_report(results: Sequence[FeasibilityResult]) -> FeasibilityReport:
    """Summarise pilot outcomes and apply the non-negotiable production gate."""

    controls = [result for result in results if result.is_control]
    residual = [result for result in results if not result.is_control]
    parser_ok = sum(result.parser_success for result in results)
    control_exact = sum(
        result.parser_success and result.exact_boundary_agreement for result in controls
    )
    proposed_control_detected = sum(
        result.proposed_safe and result.contamination_status == "detected" for result in controls
    )
    proposed_control_unknown = sum(
        result.proposed_safe and result.contamination_status == "unknown" for result in controls
    )
    proposed_detected = sum(
        result.proposed_safe and result.contamination_status == "detected" for result in results
    )
    proposed_unknown = sum(
        result.proposed_safe and result.contamination_status == "unknown" for result in results
    )
    safe_residual = [
        result
        for result in residual
        if result.proposed_safe and result.contamination_status == "clean"
    ]

    subgroup_members: dict[str, list[FeasibilityResult]] = defaultdict(list)
    for result in results:
        if result.b_status:
            subgroup_members[f"b_status:{result.b_status}"].append(result)
        if result.lifecycle:
            subgroup_members[f"lifecycle:{result.lifecycle}"].append(result)
    parser_subgroups = {
        name: {
            "count": len(members),
            "parser_success_count": sum(item.parser_success for item in members),
            "parser_success_rate": _rate(
                sum(item.parser_success for item in members), len(members)
            ),
            "proposed_safe_contamination_detected_count": sum(
                item.proposed_safe and item.contamination_status == "detected" for item in members
            ),
            "proposed_safe_contamination_unknown_count": sum(
                item.proposed_safe and item.contamination_status == "unknown" for item in members
            ),
        }
        for name, members in sorted(subgroup_members.items())
    }
    residual_by_status: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "safe": 0})
    residual_by_lifecycle: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "safe": 0})
    residual_failures: Counter[str] = Counter()
    for result in residual:
        status = result.b_status or "unobserved"
        lifecycle = result.lifecycle or "unobserved"
        residual_by_status[status]["count"] += 1
        residual_by_lifecycle[lifecycle]["count"] += 1
        if result.proposed_safe and result.contamination_status == "clean":
            residual_by_status[status]["safe"] += 1
            residual_by_lifecycle[lifecycle]["safe"] += 1
        residual_failures.update(result.failure_reasons)

    overall = {
        "count": len(results),
        "parser_success_count": parser_ok,
        "parser_success_rate": _rate(parser_ok, len(results)),
    }
    control_boundary_agreement_rate = _rate(control_exact, len(controls))
    control_summary: dict[str, Any] = {
        "count": len(controls),
        "exact_boundary_agreement_count": control_exact,
        "exact_boundary_agreement_rate": control_boundary_agreement_rate,
        "contamination_status_counts": _contamination_counts(controls),
        "proposed_safe_contamination_detected_count": proposed_control_detected,
        "proposed_safe_contamination_unknown_count": proposed_control_unknown,
    }
    residual_summary: dict[str, Any] = {
        "count": len(residual),
        "unique_safe_count": len(safe_residual),
        "unique_safe_yield": _rate(len(safe_residual), len(residual)),
        "contamination_status_counts": _contamination_counts(residual),
        "by_b_status": dict(sorted(residual_by_status.items())),
        "by_lifecycle": dict(sorted(residual_by_lifecycle.items())),
        "failure_reasons": dict(sorted(residual_failures.items())),
    }

    gate_reasons: list[str] = []
    if len(results) != 200:
        gate_reasons.append("pilot_count_not_200")
    if len(controls) != 40:
        gate_reasons.append("control_count_not_40")
    if overall["parser_success_rate"] < 0.95:
        gate_reasons.append("parser_success_below_95_percent")
    for name, metrics in parser_subgroups.items():
        if metrics["count"] >= 10 and metrics["parser_success_rate"] < 0.90:
            gate_reasons.append(f"parser_subgroup_below_90_percent:{name}")
        if metrics["proposed_safe_contamination_detected_count"]:
            gate_reasons.append(f"subgroup_contamination_detected:{name}")
        if metrics["proposed_safe_contamination_unknown_count"]:
            gate_reasons.append(f"subgroup_contamination_unknown:{name}")
    if control_boundary_agreement_rate < 0.99:
        gate_reasons.append("control_boundary_agreement_below_99_percent")
    if proposed_control_detected:
        gate_reasons.append("proposed_control_contamination_detected")
    if proposed_control_unknown:
        gate_reasons.append("proposed_control_contamination_unknown")
    if proposed_detected:
        gate_reasons.append("proposed_safe_contamination_detected")
    if proposed_unknown:
        gate_reasons.append("proposed_safe_contamination_unknown")
    if len(safe_residual) < 10:
        gate_reasons.append("residual_safe_count_below_10")
    if residual_summary["unique_safe_yield"] < 0.05:
        gate_reasons.append("residual_safe_yield_below_5_percent")
    return FeasibilityReport(
        overall=overall,
        controls=control_summary,
        residual=residual_summary,
        parser_subgroups=parser_subgroups,
        gate=FeasibilityGate(not gate_reasons, tuple(gate_reasons)),
    )


__all__ = [
    "PROVENANCE_TAG",
    "STRATA",
    "ContaminationStatus",
    "FeasibilityGate",
    "FeasibilityReport",
    "FeasibilityResult",
    "FeasibilitySample",
    "RenderedComment",
    "RenderedMappingDecision",
    "RenderedMappingEvidence",
    "SampleSelection",
    "evaluate_rendered_mapping",
    "feasibility_report",
    "normalise_visible_text",
    "select_feasibility_sample",
]
