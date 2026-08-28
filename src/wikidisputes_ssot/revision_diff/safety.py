"""Explicit, target-independent safety contract for Method-B candidates."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from wikidisputes_ssot.promotion_safety import comparison_tokens, structural_flags

METHOD_B_SAFETY_VERSION = "method-b-safety-v2"

MethodBStatus = Literal[
    "b_safe",
    "b_usable",
    "b_review",
    "b_no_candidate",
    "b_unavailable",
    "b_ambiguous",
    "b_not_applicable",
]

# These warnings describe harmless signature residue in an otherwise complete,
# independently bounded comment.  They are deliberately not ``safe``:
# b_usable remains validation-only and is never eligible for selection.
SOFT_USABILITY_REASONS = {
    "structure:terminal_signature",
    "structure:unsigned_signature_residue",
}

CRITICAL_TOKENS = {
    "not",
    "n't",
    "never",
    "no",
    "none",
    "neither",
    "nor",
    "nothing",
    "nobody",
    "nowhere",
    "cannot",
    "can't",
    "without",
    "hardly",
    "scarcely",
    "%",
    "+",
    "*",
    "/",
    "^",
    "=",
    "==",
    "!=",
    "<",
    ">",
    "<=",
    ">=",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}


@dataclass(frozen=True)
class MethodBSafetyPolicy:
    """Versioned requirements, not an empirically calibrated aggregate score."""

    require_predecessor_for: tuple[str, ...] = (
        "creation",
        "addition",
        "modification",
        "restoration",
    )
    require_signature_when_expected: bool = True
    require_offset_consistency_when_informative: bool = True
    allow_unsigned_with_closed_structural_boundaries: bool = True
    safety_version: str = METHOD_B_SAFETY_VERSION


@dataclass(frozen=True)
class MethodBSafetyDecision:
    status: MethodBStatus
    reason_codes: tuple[str, ...]
    requirements_satisfied: tuple[str, ...]
    ambiguity_flags: tuple[str, ...]
    structural_flags: tuple[str, ...]
    missing_critical_tokens: tuple[str, ...]
    added_critical_tokens: tuple[str, ...]
    policy_version: str = METHOD_B_SAFETY_VERSION

    @property
    def safe(self) -> bool:
        return self.status == "b_safe"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_row(self) -> dict[str, Any]:
        row = self.to_dict()
        for field in (
            "reason_codes",
            "requirements_satisfied",
            "ambiguity_flags",
            "structural_flags",
            "missing_critical_tokens",
            "added_critical_tokens",
        ):
            row[f"{field}_json"] = json.dumps(row.pop(field), ensure_ascii=False)
        return row


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _bool(evidence: Mapping[str, Any], key: str) -> bool:
    value = evidence.get(key)
    if isinstance(value, bool):
        return value
    return _text(value).strip().casefold() in {"1", "true", "yes"}


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return tuple(str(item) for item in parsed if str(item))
        return tuple(part for part in value.split("|") if part)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if str(item))
    return (str(value),)


def _critical(token: str) -> bool:
    return (
        token in CRITICAL_TOKENS
        or any(character.isdigit() for character in token)
        or token.startswith(("http://", "https://"))
    )


def critical_token_contradictions(
    informative_fragment: str, candidate_fragment: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compare only an explicitly aligned informative fragment.

    Whole-target similarity is intentionally absent. Callers must set
    ``informative_fragment_aligned`` before these differences become a veto.
    Multiplicity is retained so that a changed number/negation is observable.
    """

    trusted = Counter(
        token for token in comparison_tokens(informative_fragment) if _critical(token)
    )
    candidate = Counter(
        token for token in comparison_tokens(candidate_fragment) if _critical(token)
    )
    missing = tuple(sorted((trusted - candidate).elements()))
    added = tuple(sorted((candidate - trusted).elements()))
    return missing, added


def assess_method_b_safety(
    evidence: Mapping[str, Any],
    *,
    policy: MethodBSafetyPolicy | None = None,
) -> MethodBSafetyDecision:
    """Fail closed over the independent Method-B evidence chain.

    ``target_text`` may corroborate a result, but low or undefined whole-target
    similarity is never examined. A contradiction can veto only when the caller
    identifies an aligned, informative fragment.
    """

    policy = policy or MethodBSafetyPolicy()
    lifecycle = _text(evidence.get("action_type")).casefold()
    ambiguity = tuple(sorted(set(_strings(evidence.get("ambiguity_flags")))))
    candidate = _text(evidence.get("candidate_raw"))
    body = _text(evidence.get("candidate_body"))
    flags = tuple(
        sorted(set(structural_flags(body) + _strings(evidence.get("structural_warnings"))))
    )
    reasons: list[str] = []
    satisfied: list[str] = []

    if lifecycle == "deletion":
        return MethodBSafetyDecision(
            status="b_not_applicable",
            reason_codes=("target_comment_not_applicable_for_deletion",),
            requirements_satisfied=("predecessor_side_evidence_retained",)
            if _bool(evidence, "predecessor_side_evidence_retained")
            else (),
            ambiguity_flags=ambiguity,
            structural_flags=flags,
            missing_critical_tokens=(),
            added_critical_tokens=(),
            policy_version=policy.safety_version,
        )

    target_available = _text(evidence.get("target_availability")) in {
        "available",
        "content_available",
        "exact_empty_root",
    }
    predecessor_available = _text(evidence.get("predecessor_availability")) in {
        "available",
        "content_available",
        "exact_empty_root",
    }
    if not target_available:
        reasons.append("target_content_unavailable")
    else:
        satisfied.append("target_content_available")
    if lifecycle in policy.require_predecessor_for and not predecessor_available:
        reasons.append("predecessor_content_unavailable")
    elif predecessor_available:
        satisfied.append("true_predecessor_content_available")

    required_booleans = (
        ("source_provenance_exact", "source_provenance_mismatch"),
        ("revision_metadata_exact", "revision_metadata_unproven"),
        ("parentid_verified", "true_parent_unverified"),
        ("local_hashes_verified", "local_content_hash_unverified"),
        ("deterministic_diff_available", "diff_unavailable"),
        ("changed_span_in_single_candidate", "changed_span_not_in_one_comment"),
        ("assignment_unique", "assignment_not_unique"),
        ("assignment_uncontested", "assignment_contested"),
        ("lifecycle_consistent", "lifecycle_inconsistent"),
        ("boundary_defensible", "boundary_not_defensible"),
    )
    for key, reason in required_booleans:
        if _bool(evidence, key):
            satisfied.append(key)
        else:
            reasons.append(reason)

    if not candidate.strip() or not body.strip():
        reasons.append("no_candidate")
    if _bool(evidence, "adjacent_comment_contamination"):
        reasons.append("adjacent_comment_contamination")
    if _bool(evidence, "neighboring_comment_overlap"):
        reasons.append("neighboring_comment_overlap")
    if _bool(evidence, "page_level_structure_absorbed"):
        reasons.append("page_level_structure_absorbed")
    disallowed_flags = {
        "section_heading",
        "page_template",
        "terminal_signature",
        "unsigned_signature_residue",
        "empty_candidate",
    }
    for flag in flags:
        if flag in disallowed_flags:
            reasons.append(f"structure:{flag}")

    signature_expected = _bool(evidence, "signature_expected")
    if policy.require_signature_when_expected and signature_expected:
        if _bool(evidence, "signature_timestamp_consistent"):
            satisfied.append("signature_timestamp_consistent")
        elif not (
            policy.allow_unsigned_with_closed_structural_boundaries
            and _bool(evidence, "explicit_unsigned_comment")
            and _bool(evidence, "closed_structural_boundaries")
        ):
            reasons.append("signature_or_timestamp_inconsistent")

    if policy.require_offset_consistency_when_informative and _bool(
        evidence, "action_offset_informative"
    ):
        if _bool(evidence, "action_offset_consistent"):
            satisfied.append("action_offset_consistent")
        else:
            reasons.append("action_offset_contradiction")

    if lifecycle == "modification":
        if _bool(evidence, "predecessor_target_comment_continuity"):
            satisfied.append("predecessor_target_comment_continuity")
        else:
            reasons.append("modification_continuity_unproven")
    if lifecycle == "restoration":
        if _bool(evidence, "restoration_reintroduction_verified") and _bool(
            evidence, "restoration_history_sufficient"
        ):
            satisfied.append("restoration_history_verified")
        else:
            reasons.append("restoration_history_insufficient")

    informative = _text(evidence.get("informative_fragment"))
    aligned_candidate = _text(evidence.get("aligned_candidate_fragment"))
    missing_critical: tuple[str, ...] = ()
    added_critical: tuple[str, ...] = ()
    if informative and _bool(evidence, "informative_fragment_aligned"):
        missing_critical, added_critical = critical_token_contradictions(
            informative, aligned_candidate
        )
        if missing_critical:
            reasons.append("critical_token_contradiction_missing")
        if added_critical:
            reasons.append("critical_token_contradiction_added")

    if ambiguity:
        reasons.append("ambiguity_flags_present")
    ordered_reasons = tuple(dict.fromkeys(reasons))
    if "no_candidate" in ordered_reasons:
        status: MethodBStatus = "b_no_candidate"
    elif any(reason.endswith("unavailable") for reason in ordered_reasons):
        status = "b_unavailable"
    elif ambiguity or any(
        reason in {"assignment_not_unique", "assignment_contested"} for reason in ordered_reasons
    ):
        status = "b_ambiguous"
    elif not ordered_reasons:
        status = "b_safe"
    elif set(ordered_reasons).issubset(SOFT_USABILITY_REASONS):
        status = "b_usable"
    else:
        status = "b_review"
    return MethodBSafetyDecision(
        status=status,
        reason_codes=ordered_reasons,
        requirements_satisfied=tuple(dict.fromkeys(satisfied)),
        ambiguity_flags=ambiguity,
        structural_flags=flags,
        missing_critical_tokens=missing_critical,
        added_critical_tokens=added_critical,
        policy_version=policy.safety_version,
    )
