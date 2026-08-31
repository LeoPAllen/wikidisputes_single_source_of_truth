"""Pure revision-level Method-B reconstruction orchestration."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from .assignment import (
    DEFAULT_ASSIGNMENT_CONFIG,
    AssignmentConfig,
    assign_revision_actions,
)
from .boundaries import BoundaryCandidate, extract_comment_candidates
from .diff import DiffResourceLimitError, align_revisions
from .diff_span_boundary import diff_span_structural
from .localization import LocalizedCandidates, localize_candidates
from .models import (
    DiffOpKind,
    MethodBEvidence,
    RevisionAvailability,
    RevisionDiff,
    RevisionText,
    local_content_sha256,
)
from .safety import assess_method_b_safety
from .token_persistence import TokenPersistenceResult, token_persistence_continuity


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    return value is True or _text(value).strip().casefold() in {"1", "true", "yes"}


def _json_list(values: Sequence[Any]) -> str:
    return json.dumps(list(values), ensure_ascii=False, sort_keys=True, default=str)


def _source_uids(action: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    if action.get("source_row_uid"):
        values.append(str(action["source_row_uid"]))
    raw = action.get("source_row_uids_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            values.extend(str(item) for item in parsed if item)
    values.extend(str(item) for item in action.get("source_occurrence_uids", ()) if item)
    return tuple(sorted(set(values)))


def action_offset_hint(action_id: Any) -> int | None:
    """Parse the WikiConv positional component only as an uncalibrated hint."""

    parts = _text(action_id).split(".")
    return _int(parts[1]) if len(parts) > 1 else None


def _operation_rows(revision_diff: RevisionDiff) -> list[dict[str, Any]]:
    return [
        {
            "kind": operation.kind.value,
            "predecessor_token_start": operation.predecessor_tokens.start,
            "predecessor_token_end": operation.predecessor_tokens.end,
            "target_token_start": operation.target_tokens.start,
            "target_token_end": operation.target_tokens.end,
            "predecessor_char_start": operation.predecessor_chars.start,
            "predecessor_char_end": operation.predecessor_chars.end,
            "target_char_start": operation.target_chars.start,
            "target_char_end": operation.target_chars.end,
        }
        for operation in revision_diff.operations
    ]


def _changed_target_spans(revision_diff: RevisionDiff) -> list[tuple[int, int]]:
    return [
        (span.start, span.end)
        for operation, span in zip(
            (op for op in revision_diff.operations if op.kind is not DiffOpKind.EQUAL),
            revision_diff.changed.target_chars,
            strict=True,
        )
        if operation.kind in {DiffOpKind.INSERT, DiffOpKind.REPLACE}
    ]


def _changed_predecessor_spans(revision_diff: RevisionDiff) -> list[tuple[int, int]]:
    return [
        (span.start, span.end)
        for operation, span in zip(
            (op for op in revision_diff.operations if op.kind is not DiffOpKind.EQUAL),
            revision_diff.changed.predecessor_chars,
            strict=True,
        )
        if operation.kind in {DiffOpKind.DELETE, DiffOpKind.REPLACE}
    ]


def _span_inside(span: tuple[int, int], candidate: BoundaryCandidate) -> bool:
    if span[0] == span[1]:
        return candidate.start <= span[0] <= candidate.end
    return candidate.start <= span[0] and span[1] <= candidate.end


def _substantive_spans(raw: str, spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Keep positive-width spans containing non-whitespace target text."""
    return [
        span
        for span in spans
        if span[0] < span[1] and raw[span[0] : span[1]].strip()
    ]


def _span_inside_with_structural_whitespace(
    span: tuple[int, int], candidate: BoundaryCandidate, raw: str
) -> bool:
    """Permit only boundary whitespace outside a structurally closed candidate."""

    if _span_inside(span, candidate):
        return True
    overlap_start = max(span[0], candidate.start)
    overlap_end = min(span[1], candidate.end)
    if overlap_start >= overlap_end:
        return False
    outside_left = raw[span[0] : overlap_start]
    outside_right = raw[overlap_end : span[1]]
    return not outside_left.strip() and not outside_right.strip()


def _candidate_by_uid(
    candidates: Sequence[BoundaryCandidate], candidate_uid: str | None
) -> BoundaryCandidate | None:
    return next(
        (candidate for candidate in candidates if candidate.candidate_uid == candidate_uid),
        None,
    )


def _words(value: Any) -> set[str]:
    return set(re.findall(r"\w+", _text(value).casefold()))


def _candidate_support(
    action: Mapping[str, Any],
    candidate: BoundaryCandidate,
    revision_diff: RevisionDiff,
    predecessor_candidates: Sequence[BoundaryCandidate],
) -> tuple[tuple[int, ...], tuple[str, ...], bool]:
    """Return conservative action/candidate evidence for hunk attribution."""

    evidence: list[str] = []
    source = _text(action.get("source_text")).strip()
    body = candidate.body_wikitext.strip()
    if source and source == body:
        evidence.append("informative_source_text_exact_candidate_body")
    source_words = _words(source)
    candidate_words = _words(body)
    shared = source_words & candidate_words
    if source_words and len(shared) >= min(3, len(source_words)):
        evidence.append("informative_source_tokens_observed_in_candidate")
    speaker = _text(action.get("wikiconv_speaker")).strip()
    if (
        speaker
        and candidate.signature_user_target
        and speaker.casefold() == candidate.signature_user_target.casefold()
    ):
        evidence.append("frozen_speaker_matches_signature_target")
    offset = action_offset_hint(action.get("action_id_exact"))
    if offset is not None and candidate.start <= offset < candidate.end:
        evidence.append("wikiconv_offset_hint_inside_candidate")

    lifecycle = _text(action.get("action_type")).casefold()
    if lifecycle == "modification":
        continuity, _ = _structural_continuity(revision_diff, candidate, predecessor_candidates)
        if continuity:
            evidence.append("modification_structural_continuity")
    elif lifecycle == "restoration":
        history_hashes = {str(value) for value in action.get("restoration_history_body_hashes", ())}
        candidate_hash = local_content_sha256(candidate.body_wikitext)
        predecessor_hashes = {
            local_content_sha256(item.body_wikitext) for item in predecessor_candidates
        }
        if candidate_hash in history_hashes and candidate_hash not in predecessor_hashes:
            evidence.append("restoration_history_body_match")

    rank = (
        int("informative_source_text_exact_candidate_body" in evidence),
        int("informative_source_tokens_observed_in_candidate" in evidence),
        int(
            "modification_structural_continuity" in evidence
            or "restoration_history_body_match" in evidence
        ),
        int("frozen_speaker_matches_signature_target" in evidence),
        int("wikiconv_offset_hint_inside_candidate" in evidence),
    )
    # Offsets are hints only and can never manufacture attribution on their own.
    decisive = any(rank[:-1])
    return rank, tuple(evidence), decisive


def _action_specific_spans(
    actions: Sequence[Mapping[str, Any]],
    target_spans: Sequence[tuple[int, int]],
    localization: LocalizedCandidates,
    revision_diff: RevisionDiff,
    predecessor_candidates: Sequence[BoundaryCandidate],
) -> tuple[
    dict[str, list[tuple[int, int]]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[str]],
]:
    """Narrow multi-action hunks only under mutual unique candidate support."""

    action_ids = [_text(action.get("action_uid")) for action in actions]
    if len(actions) == 1:
        # A single action owns the complete revision-local substantive span
        # set. Localization only controls candidate visibility; it must not
        # silently discard an unlocalized hunk from safety/lifecycle checks.
        spans = sorted(set(target_spans))
        return (
            {action_ids[0]: spans},
            {
                action_ids[0]: [
                    {
                        "reason": "single_action_all_relevant_localized_spans",
                        "target_spans": spans,
                    }
                ]
            },
            {action_ids[0]: [candidate.candidate_uid for candidate in localization.candidates]},
        )

    support: dict[tuple[str, str], tuple[tuple[int, ...], tuple[str, ...], bool]] = {}
    by_uid = {candidate.candidate_uid: candidate for candidate in localization.candidates}
    for action in actions:
        action_uid = _text(action.get("action_uid"))
        for candidate in localization.candidates:
            support[(action_uid, candidate.candidate_uid)] = _candidate_support(
                action, candidate, revision_diff, predecessor_candidates
            )

    action_choice: dict[str, str] = {}
    for action_uid in action_ids:
        ranked = [
            (support[(action_uid, candidate_uid)][0], candidate_uid)
            for candidate_uid in by_uid
            if support[(action_uid, candidate_uid)][2]
        ]
        if not ranked:
            continue
        best = max(rank for rank, _ in ranked)
        choices = [candidate_uid for rank, candidate_uid in ranked if rank == best]
        if len(choices) == 1:
            action_choice[action_uid] = choices[0]

    candidate_choice: dict[str, str] = {}
    for candidate_uid in by_uid:
        ranked = [
            (support[(action_uid, candidate_uid)][0], action_uid)
            for action_uid in action_ids
            if support[(action_uid, candidate_uid)][2]
        ]
        if not ranked:
            continue
        best = max(rank for rank, _ in ranked)
        choices = [action_uid for rank, action_uid in ranked if rank == best]
        if len(choices) == 1:
            candidate_choice[candidate_uid] = choices[0]

    owned = {
        candidate_uid: action_uid
        for action_uid, candidate_uid in action_choice.items()
        if candidate_choice.get(candidate_uid) == action_uid
    }
    spans_by_candidate: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for match in localization.matches:
        spans_by_candidate[match.candidate_uid].add(match.target_span)

    spans_by_action: dict[str, list[tuple[int, int]]] = {}
    evidence_by_action: dict[str, list[dict[str, Any]]] = {}
    candidates_by_action: dict[str, list[str]] = {}
    for action_uid in action_ids:
        owned_candidates = sorted(
            candidate_uid for candidate_uid, owner in owned.items() if owner == action_uid
        )
        if owned_candidates:
            spans = sorted(
                {
                    span
                    for candidate_uid in owned_candidates
                    for span in spans_by_candidate[candidate_uid]
                }
            )
            entries = [
                {
                    "reason": "mutual_unique_action_candidate_support",
                    "candidate_uid": candidate_uid,
                    "target_spans": sorted(spans_by_candidate[candidate_uid]),
                    "support": support[(action_uid, candidate_uid)][1],
                }
                for candidate_uid in owned_candidates
            ]
            allowed_candidates = owned_candidates
        else:
            unclaimed_candidates = sorted(
                candidate_uid for candidate_uid in by_uid if candidate_uid not in owned
            )
            unclaimed = {
                span
                for candidate_uid, candidate_spans in spans_by_candidate.items()
                if candidate_uid in unclaimed_candidates
                for span in candidate_spans
            }
            spans = sorted(unclaimed)
            entries = [
                {
                    "reason": "no_mutual_unique_hunk_attribution",
                    "target_spans": spans,
                }
            ]
            allowed_candidates = unclaimed_candidates
        spans_by_action[action_uid] = spans
        evidence_by_action[action_uid] = entries
        candidates_by_action[action_uid] = allowed_candidates
    return spans_by_action, evidence_by_action, candidates_by_action


def neighboring_comment_contamination_status(
    candidate: BoundaryCandidate | None,
    page_candidates: Sequence[BoundaryCandidate],
    page_raw: str,
) -> str:
    """Measure overlap/absorption of another independently signed comment."""

    if candidate is None:
        return "unknown"
    if (
        candidate.start < 0
        or candidate.end > len(page_raw)
        or candidate.start >= candidate.end
        or page_raw[candidate.start : candidate.end] != candidate.raw_wikitext
    ):
        return "unknown"
    for other in page_candidates:
        if other.candidate_uid == candidate.candidate_uid:
            continue
        if candidate.start < other.end and other.start < candidate.end:
            return "detected"
        if (
            other.signature_start is not None
            and candidate.start <= other.signature_start < candidate.end
        ):
            return "detected"
    parsed_inside = extract_comment_candidates(candidate.raw_wikitext)
    if len(parsed_inside) > 1:
        return "detected"
    if candidate.boundary_warnings or len(parsed_inside) != 1:
        return "unknown"
    return "clean"


def _structural_continuity(
    revision_diff: RevisionDiff,
    target_candidate: BoundaryCandidate,
    predecessor_candidates: Sequence[BoundaryCandidate],
) -> tuple[bool, BoundaryCandidate | None]:
    """Require an equal aligned region inside structurally compatible comments."""

    for predecessor_candidate in predecessor_candidates:
        same_signature = bool(
            target_candidate.signature_user_target
            and predecessor_candidate.signature_user_target
            and target_candidate.signature_user_target.casefold()
            == predecessor_candidate.signature_user_target.casefold()
        )
        same_timestamp = bool(
            target_candidate.signature_timestamp
            and target_candidate.signature_timestamp == predecessor_candidate.signature_timestamp
        )
        if not (same_signature or same_timestamp):
            continue
        for operation in revision_diff.operations:
            if operation.kind is not DiffOpKind.EQUAL:
                continue
            if operation.predecessor_chars.length == 0 or operation.target_chars.length == 0:
                continue
            predecessor_overlap = max(
                0,
                min(predecessor_candidate.body_end, operation.predecessor_chars.end)
                - max(predecessor_candidate.body_start, operation.predecessor_chars.start),
            )
            target_overlap = max(
                0,
                min(target_candidate.body_end, operation.target_chars.end)
                - max(target_candidate.body_start, operation.target_chars.start),
            )
            if predecessor_overlap > 0 and target_overlap > 0:
                return True, predecessor_candidate
    return False, None


def _base_evidence(
    action: Mapping[str, Any],
    source_row_uid: str,
    target: RevisionText,
    predecessor: RevisionText,
    *,
    page_id: str | None,
    target_response_hash: str | None,
    predecessor_response_hash: str | None,
    target_content_pointer: str | None,
    predecessor_content_pointer: str | None,
) -> MethodBEvidence:
    return MethodBEvidence(
        source_row_uid=source_row_uid,
        logical_utterance_uid=_text(action.get("logical_utterance_uid")),
        action_uid=_text(action.get("action_uid")),
        action_type=_text(action.get("action_type")),
        target_revision_id=target.revision_id,
        predecessor_revision_id=predecessor.revision_id or None,
        page_id=page_id,
        target_availability=target.availability.value,
        predecessor_availability=predecessor.availability.value,
        target_api_sha1=target.api_sha1,
        predecessor_api_sha1=predecessor.api_sha1,
        target_local_content_sha256=target.local_content_sha256,
        predecessor_local_content_sha256=predecessor.local_content_sha256,
        target_response_hash=target_response_hash,
        predecessor_response_hash=predecessor_response_hash,
        target_content_pointer=target_content_pointer,
        predecessor_content_pointer=predecessor_content_pointer,
        revision_actor=_text(action.get("revision_actor")) or None,
        wikiconv_speaker=_text(action.get("wikiconv_speaker")) or None,
        wikidisputes_speaker=_text(action.get("wikidisputes_speaker")) or None,
        action_offset_hint=action_offset_hint(action.get("action_id_exact")),
        action_count=int(action.get("action_count", 1)),
        status="b_review",
        reason_codes_json="[]",
    )


def _unavailable_rows(
    actions: Sequence[Mapping[str, Any]],
    target: RevisionText,
    predecessor: RevisionText,
    **metadata: Any,
) -> list[MethodBEvidence]:
    output: list[MethodBEvidence] = []
    reasons = []
    if target.availability is not RevisionAvailability.AVAILABLE:
        reasons.append("target_content_unavailable")
    if predecessor.availability is not RevisionAvailability.AVAILABLE:
        reasons.append("predecessor_content_unavailable")
    for action in actions:
        for source_uid in _source_uids(action):
            base = _base_evidence(action, source_uid, target, predecessor, **metadata)
            lifecycle = _text(action.get("action_type"))
            status = "b_not_applicable" if lifecycle == "deletion" else "b_unavailable"
            action_reasons = (
                ["target_comment_not_applicable_for_deletion", *reasons]
                if lifecycle == "deletion"
                else reasons
            )
            output.append(
                replace(base, status=status, reason_codes_json=_json_list(action_reasons))
            )
    return output


def recover_revision_actions(
    actions: Sequence[Mapping[str, Any]],
    predecessor: RevisionText,
    target: RevisionText,
    *,
    page_id: str | None = None,
    target_response_hash: str | None = None,
    predecessor_response_hash: str | None = None,
    target_content_pointer: str | None = None,
    predecessor_content_pointer: str | None = None,
    assignment_config: AssignmentConfig = DEFAULT_ASSIGNMENT_CONFIG,
    max_trace_cells: int = 2_000_000,
) -> list[MethodBEvidence]:
    """Recover all actions sharing one revision as a single assignment problem."""

    metadata = {
        "page_id": page_id,
        "target_response_hash": target_response_hash,
        "predecessor_response_hash": predecessor_response_hash,
        "target_content_pointer": target_content_pointer,
        "predecessor_content_pointer": predecessor_content_pointer,
    }
    if (
        target.availability is not RevisionAvailability.AVAILABLE
        or predecessor.availability is not RevisionAvailability.AVAILABLE
    ):
        return _unavailable_rows(actions, target, predecessor, **metadata)

    try:
        revision_diff = align_revisions(predecessor, target, max_trace_cells=max_trace_cells)
    except DiffResourceLimitError:
        rows = _unavailable_rows(actions, target, predecessor, **metadata)
        return [
            replace(
                row,
                status="b_review",
                reason_codes_json=_json_list(["diff_operational_resource_limit"]),
            )
            for row in rows
        ]

    target_raw = target.raw_text or ""
    predecessor_raw = predecessor.raw_text or ""
    parsed_target_candidates = extract_comment_candidates(target_raw)
    target_candidates = parsed_target_candidates
    predecessor_candidates = extract_comment_candidates(predecessor_raw)
    target_spans = _changed_target_spans(revision_diff)
    predecessor_spans = _changed_predecessor_spans(revision_diff)
    localization = localize_candidates(target_raw, target_candidates, target_spans)

    # Multiple source occurrences may point to the same frozen action. Assign
    # that action once, then project the same evidence to each occurrence.
    by_action_uid: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for action in actions:
        by_action_uid[_text(action.get("action_uid"))].append(action)
    unique_actions: list[dict[str, Any]] = []
    for action_uid in sorted(by_action_uid):
        representative = dict(by_action_uid[action_uid][0])
        representative["offset_hint"] = action_offset_hint(representative.get("action_id_exact"))
        representative["informative_text"] = representative.get("source_text")
        representative["speaker"] = representative.get("wikiconv_speaker")
        unique_actions.append(representative)

    assignable = [
        action for action in unique_actions if _text(action.get("action_type")) != "deletion"
    ]
    diff_span_fallback = False
    if localization.localized_candidate_count == 0 and len(assignable) == 1:
        structural_candidate = diff_span_structural(target_raw, target_spans)
        if structural_candidate is not None:
            # Parsed candidates are retained as hard barriers and remain the
            # preferred representation. The fallback is additive only when
            # localization found no parsed target candidate.
            target_candidates = [*parsed_target_candidates, structural_candidate]
            localization = localize_candidates(target_raw, target_candidates, target_spans)
            diff_span_fallback = structural_candidate.candidate_uid in {
                candidate.candidate_uid for candidate in localization.candidates
            }
    localized_candidates = list(localization.candidates)
    action_spans, hunk_evidence, action_candidates = (
        _action_specific_spans(
            assignable,
            target_spans,
            localization,
            revision_diff,
            predecessor_candidates,
        )
        if assignable
        else ({}, {}, {})
    )
    for action in assignable:
        action_uid = _text(action.get("action_uid"))
        action["changed_ranges"] = action_spans.get(action_uid, [])
        action["localized_candidate_uids"] = action_candidates.get(action_uid, [])
    assignments = {
        result.action_uid: result
        for result in assign_revision_actions(
            assignable, localized_candidates, config=assignment_config
        )
    }
    operations_json = json.dumps(_operation_rows(revision_diff), ensure_ascii=False, sort_keys=True)
    predecessor_ranges_json = _json_list(predecessor_spans)
    target_ranges_json = _json_list(target_spans)
    output: list[MethodBEvidence] = []

    for action in actions:
        action_uid = _text(action.get("action_uid"))
        lifecycle = _text(action.get("action_type"))
        source_uids = _source_uids(action)
        if not source_uids:
            continue
        assignment = assignments.get(action_uid)
        candidate = _candidate_by_uid(
            localized_candidates, assignment.candidate_uid if assignment else None
        )
        continuity = False
        token_continuity: TokenPersistenceResult | None = None
        predecessor_candidate: BoundaryCandidate | None = None
        if candidate is not None and lifecycle == "modification":
            continuity, predecessor_candidate = _structural_continuity(
                revision_diff, candidate, predecessor_candidates
            )
            if not continuity and len(assignable) == 1:
                proposed_continuity = token_persistence_continuity(
                    revision_diff,
                    candidate,
                    predecessor_candidates,
                    action_spans.get(action_uid, ()),
                )
                if (
                    proposed_continuity.verified
                    and proposed_continuity.predecessor_candidate is not None
                    and neighboring_comment_contamination_status(
                        proposed_continuity.predecessor_candidate,
                        predecessor_candidates,
                        predecessor_raw,
                    )
                    == "clean"
                ):
                    continuity = True
                    token_continuity = proposed_continuity
                    predecessor_candidate = proposed_continuity.predecessor_candidate

        if lifecycle == "deletion":
            matching_predecessors = [
                item
                for item in predecessor_candidates
                if any(_span_inside(span, item) for span in predecessor_spans)
            ]
            if len(matching_predecessors) == 1:
                predecessor_candidate = matching_predecessors[0]

        restoration_history_hashes = {
            str(value) for value in action.get("restoration_history_body_hashes", ())
        }
        predecessor_body_hashes = {
            local_content_sha256(item.body_wikitext) for item in predecessor_candidates
        }
        candidate_body_hash = local_content_sha256(candidate.body_wikitext) if candidate else None
        restoration_reintroduced = bool(
            lifecycle == "restoration"
            and candidate_body_hash
            and candidate_body_hash in restoration_history_hashes
            and candidate_body_hash not in predecessor_body_hashes
        )
        restoration_history_sufficient = bool(
            lifecycle == "restoration"
            and _bool(action.get("restoration_history_complete"))
            and restoration_history_hashes
        )
        structural_fallback = bool(
            diff_span_fallback
            and candidate is not None
            and "diff_span_structural" in candidate.boundary_evidence
        )
        exact_source_body_corroboration = bool(
            structural_fallback
            and _text(action.get("source_text")).strip()
            and candidate is not None
            and _text(action.get("source_text")).strip() == candidate.body_wikitext.strip()
        )

        for source_uid in source_uids:
            base = _base_evidence(action, source_uid, target, predecessor, **metadata)
            assignment_status = (
                "not_applicable"
                if lifecycle == "deletion"
                else (assignment.status if assignment else "unmatched")
            )
            ambiguity_flags = tuple(assignment.warnings) if assignment else ()
            contamination = (
                "clean"
                if structural_fallback and exact_source_body_corroboration
                else "unknown"
                if structural_fallback
                else neighboring_comment_contamination_status(
                    candidate, target_candidates, target_raw
                )
            )
            safety_ambiguity_flags = (
                (*ambiguity_flags, "neighboring_comment_contamination_unknown")
                if contamination == "unknown" and candidate is not None
                else ambiguity_flags
            )
            assignment_reasons: list[str] = []
            if lifecycle == "deletion":
                assignment_reasons.append("target_assignment_not_applicable_for_deletion")
            elif localization.whole_page_candidate_count == 0:
                assignment_reasons.append("zero_whole_page_structural_candidates")
                assignment_reasons.append("zero_localized_structural_candidates")
            elif localization.localized_candidate_count == 0:
                assignment_reasons.append("zero_localized_structural_candidates")
            elif assignment_status == "assigned":
                assignment_reasons.append("unique_global_assignment")
            elif assignment_status == "unmatched":
                assignment_reasons.append("localized_candidates_exist_but_unmatched")
            else:
                assignment_reasons.extend(ambiguity_flags or ("assignment_ambiguous",))
            localization_rows = [
                {
                    "candidate_uid": match.candidate_uid,
                    "target_span": match.target_span,
                    "reasons": match.reasons,
                }
                for match in localization.matches
            ]
            action_target_spans = action_spans.get(action_uid, [])
            action_hunk_evidence = list(hunk_evidence.get(action_uid, ()))
            if token_continuity is not None:
                action_hunk_evidence.append(
                    {
                        "reason": "token_persistence_continuity",
                        "qualifying_predecessor_count": (
                            token_continuity.qualifying_predecessor_count
                        ),
                        "exact_word_token_count": token_continuity.exact_word_token_count,
                        "exact_non_whitespace_char_count": (
                            token_continuity.exact_non_whitespace_char_count
                        ),
                        "adjacent_equal_operation_indices": (
                            token_continuity.adjacent_equal_operation_indices
                        ),
                    }
                )
            evidence = replace(
                base,
                diff_operations_json=operations_json,
                predecessor_changed_ranges_json=predecessor_ranges_json,
                target_changed_ranges_json=target_ranges_json,
                candidate_start=candidate.start if candidate else None,
                candidate_end=candidate.end if candidate else None,
                candidate_raw=candidate.raw_wikitext if candidate else None,
                candidate_raw_sha256=(
                    local_content_sha256(candidate.raw_wikitext) if candidate else None
                ),
                body_start=candidate.body_start if candidate else None,
                body_end=candidate.body_end if candidate else None,
                candidate_body=candidate.body_wikitext if candidate else None,
                candidate_body_sha256=(
                    local_content_sha256(candidate.body_wikitext) if candidate else None
                ),
                predecessor_candidate_start=(
                    predecessor_candidate.start if predecessor_candidate else None
                ),
                predecessor_candidate_end=(
                    predecessor_candidate.end if predecessor_candidate else None
                ),
                predecessor_candidate_raw=(
                    predecessor_candidate.raw_wikitext if predecessor_candidate else None
                ),
                predecessor_candidate_body=(
                    predecessor_candidate.body_wikitext if predecessor_candidate else None
                ),
                boundary_method=(
                    "diff_span_structural"
                    if structural_fallback
                    else "independent_signature_heading_indent_v2_weak_blank"
                    if candidate
                    else None
                ),
                boundary_evidence_json=_json_list(candidate.boundary_evidence if candidate else ()),
                boundary_warnings_json=_json_list(candidate.boundary_warnings if candidate else ()),
                signature_status="explicit_evidence_observed" if candidate else None,
                signature_raw=candidate.raw_signature_wikitext if candidate else None,
                signature_timestamp=candidate.signature_timestamp if candidate else None,
                signature_author=candidate.signature_user_target if candidate else None,
                indentation=candidate.indentation if candidate else None,
                thread_depth=candidate.depth if candidate else None,
                action_offset_consistency=(
                    "inside_candidate_uncalibrated_hint"
                    if candidate
                    and base.action_offset_hint is not None
                    and candidate.start <= base.action_offset_hint <= candidate.end
                    else "outside_candidate_uncalibrated_hint"
                    if base.action_offset_hint is not None
                    else "not_observed"
                ),
                lifecycle_consistency=(
                    "target_not_applicable_deletion"
                    if lifecycle == "deletion"
                    else "target_change_localized"
                    if candidate
                    else "unresolved"
                ),
                candidate_count=localization.localized_candidate_count,
                whole_page_candidate_count=localization.whole_page_candidate_count,
                localized_candidate_count=localization.localized_candidate_count,
                localization_evidence_json=_json_list(localization_rows),
                action_target_changed_ranges_json=_json_list(action_target_spans),
                hunk_attribution_evidence_json=_json_list(action_hunk_evidence),
                action_count=len(unique_actions),
                assignment_status=assignment_status,
                assignment_evidence_json=_json_list(assignment.evidence if assignment else ()),
                assignment_reason_codes_json=_json_list(assignment_reasons),
                assignment_conflicts_json=_json_list(ambiguity_flags),
                competing_candidates_json=_json_list(
                    item.candidate_uid for item in localized_candidates if item is not candidate
                ),
                competing_actions_json=_json_list(
                    item["action_uid"]
                    for item in unique_actions
                    if item["action_uid"] != action_uid
                ),
                ambiguity_flags_json=_json_list(safety_ambiguity_flags),
                neighboring_comment_contamination=contamination,
                structural_warnings_json=_json_list(
                    candidate.boundary_warnings if candidate else ()
                ),
                predecessor_target_continuity=(
                    "token_persistence_continuity"
                    if token_continuity is not None
                    else "verified_structural_alignment"
                    if continuity
                    else "not_required"
                    if lifecycle not in {"modification"}
                    else "unverified"
                ),
                restoration_history_status=(
                    "verified"
                    if restoration_history_sufficient and restoration_reintroduced
                    else "insufficient"
                    if lifecycle == "restoration"
                    else None
                ),
            )
            substantive_action_spans = _substantive_spans(target_raw, action_target_spans)
            contained = [
                span
                for span in substantive_action_spans
                if candidate
                and _span_inside_with_structural_whitespace(span, candidate, target_raw)
            ]
            assignment_evidence = list(assignment.evidence if assignment else ())
            if exact_source_body_corroboration:
                assignment_evidence.append("exact_source_body_corroboration")
            lifecycle_spans = [
                (operation.target_chars.start, operation.target_chars.end)
                for operation in revision_diff.operations
                if operation.kind in {DiffOpKind.INSERT, DiffOpKind.REPLACE}
                and (operation.target_chars.start, operation.target_chars.end)
                in substantive_action_spans
            ]
            safety = assess_method_b_safety(
                {
                    **evidence.to_row(),
                    "source_provenance_exact": _bool(action.get("source_provenance_exact")),
                    "revision_metadata_exact": True,
                    "parentid_verified": _bool(action.get("parentid_verified", True)),
                    "local_hashes_verified": bool(
                        target.local_content_sha256 and predecessor.local_content_sha256
                    ),
                    "deterministic_diff_available": True,
                    "changed_span_in_single_candidate": bool(
                        candidate
                        and substantive_action_spans
                        and len(contained) == len(substantive_action_spans)
                    ),
                    "assignment_unique": assignment_status == "assigned",
                    "assignment_uncontested": assignment_status == "assigned"
                    and not ambiguity_flags,
                    "lifecycle_consistent": bool(
                        lifecycle == "deletion"
                        or (
                            candidate
                            and substantive_action_spans
                            and len(lifecycle_spans) == len(substantive_action_spans)
                            and all(
                                _span_inside_with_structural_whitespace(
                                    span, candidate, target_raw
                                )
                                for span in lifecycle_spans
                            )
                        )
                    ),
                    "boundary_defensible": bool(
                        (
                            structural_fallback
                            and exact_source_body_corroboration
                        )
                        or (
                            candidate
                            and candidate.signature_timestamp
                            and candidate.signature_user_target
                            and (
                                candidate.start == 0
                                or any(
                                    item.startswith("preceded_by_")
                                    for item in candidate.boundary_evidence
                                )
                            )
                            and not candidate.boundary_warnings
                        )
                    ),
                    "signature_expected": bool(candidate) and not structural_fallback,
                    "signature_timestamp_consistent": bool(
                        candidate
                        and candidate.signature_timestamp
                        and candidate.signature_user_target
                    ),
                    "explicit_unsigned_comment": structural_fallback,
                    "closed_structural_boundaries": structural_fallback or bool(candidate),
                    "action_offset_informative": _bool(action.get("offset_coordinate_calibrated")),
                    "action_offset_consistent": evidence.action_offset_consistency
                    == "inside_candidate_uncalibrated_hint",
                    "predecessor_target_comment_continuity": continuity,
                    "restoration_reintroduction_verified": _bool(restoration_reintroduced),
                    "restoration_history_sufficient": restoration_history_sufficient,
                    "predecessor_side_evidence_retained": bool(predecessor_candidate),
                    "informative_fragment": action.get("source_text"),
                    "aligned_candidate_fragment": candidate.body_wikitext if candidate else None,
                    "informative_fragment_aligned": _bool(
                        action.get("informative_fragment_aligned")
                    ),
                    "ambiguity_flags": safety_ambiguity_flags,
                    "neighboring_comment_contamination": contamination == "detected",
                    "candidate_raw": candidate.raw_wikitext if candidate else None,
                    "candidate_body": candidate.body_wikitext if candidate else None,
                }
            )
            evidence = replace(
                evidence,
                assignment_evidence_json=_json_list(assignment_evidence),
            )
            output.append(
                replace(
                    evidence,
                    safety_version=safety.policy_version,
                    status=safety.status,
                    reason_codes_json=_json_list(safety.reason_codes),
                    critical_token_contradictions_json=_json_list(
                        [
                            *(f"missing:{token}" for token in safety.missing_critical_tokens),
                            *(f"added:{token}" for token in safety.added_critical_tokens),
                        ]
                    ),
                )
            )
    return sorted(
        output,
        key=lambda row: (row.target_revision_id, row.action_uid, row.source_row_uid),
    )


def evidence_as_rows(evidence: Sequence[MethodBEvidence]) -> list[dict[str, object]]:
    return [row.to_row() for row in evidence]
