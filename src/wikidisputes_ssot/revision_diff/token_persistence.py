"""Deterministic token-persistence continuity for revision modifications."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .boundaries import BoundaryCandidate
from .models import DiffOperation, DiffOpKind, RevisionDiff

MIN_EXACT_WORD_TOKENS = 10
MIN_EXACT_NON_WHITESPACE_CHARS = 40
_WORD_RE = re.compile(r"^\w+$", re.UNICODE)


@dataclass(frozen=True, slots=True)
class TokenPersistenceResult:
    """Auditable result of a strict exact-token continuity check."""

    verified: bool
    predecessor_candidate: BoundaryCandidate | None
    qualifying_predecessor_count: int
    exact_word_token_count: int
    exact_non_whitespace_char_count: int
    adjacent_equal_operation_indices: tuple[int, ...]
    evidence: tuple[str, ...]


def _range(value: object) -> tuple[int, int] | None:
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return int(value[0]), int(value[1])
    start = getattr(value, "start", None)
    end = getattr(value, "end", None)
    if start is None or end is None:
        return None
    return int(start), int(end)


def _positive_substantive_spans(
    spans: Sequence[object], target_raw: str
) -> tuple[list[tuple[int, int]], tuple[str, ...]]:
    substantive: list[tuple[int, int]] = []
    evidence: list[str] = []
    for value in spans:
        span = _range(value)
        if span is None or span[0] < 0 or span[1] < span[0] or span[1] > len(target_raw):
            return [], ("invalid_target_span",)
        if span[0] == span[1] or not target_raw[span[0] : span[1]].strip():
            continue
        substantive.append(span)
    if not substantive:
        evidence.append("no_substantive_target_spans")
    return substantive, tuple(evidence)


def _overlap(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int] | None:
    start, end = max(left[0], right[0]), min(left[1], right[1])
    return (start, end) if start < end else None


def _clean_predecessor(candidate: BoundaryCandidate) -> bool:
    if candidate.boundary_warnings:
        return False
    markers = (*candidate.boundary_evidence, *candidate.boundary_warnings)
    return not any(
        marker.casefold().find("contaminat") >= 0
        or marker.casefold().find("absorbed") >= 0
        for marker in markers
    )


def _equal_block_metrics(
    revision_diff: RevisionDiff,
    operation: DiffOperation,
    target_candidate: BoundaryCandidate,
    predecessor_candidate: BoundaryCandidate,
) -> tuple[int, int] | None:
    """Count exact word tokens/chars retained inside both candidate bodies."""

    target_raw = revision_diff.target.raw_text
    predecessor_raw = revision_diff.predecessor.raw_text
    if target_raw is None or predecessor_raw is None:
        return None
    target_body = (target_candidate.body_start, target_candidate.body_end)
    predecessor_body = (
        predecessor_candidate.body_start,
        predecessor_candidate.body_end,
    )
    target_overlap = _overlap(
        (operation.target_chars.start, operation.target_chars.end), target_body
    )
    predecessor_overlap = _overlap(
        (operation.predecessor_chars.start, operation.predecessor_chars.end),
        predecessor_body,
    )
    if target_overlap is None or predecessor_overlap is None:
        return None

    target_chars = target_raw[target_overlap[0] : target_overlap[1]]
    predecessor_chars = predecessor_raw[predecessor_overlap[0] : predecessor_overlap[1]]
    non_whitespace_chars = min(
        sum(not char.isspace() for char in target_chars),
        sum(not char.isspace() for char in predecessor_chars),
    )

    word_tokens = 0
    predecessor_tokens = revision_diff.predecessor_tokens
    target_tokens = revision_diff.target_tokens
    token_count = min(
        operation.predecessor_tokens.length,
        operation.target_tokens.length,
    )
    for offset in range(token_count):
        predecessor_index = operation.predecessor_tokens.start + offset
        target_index = operation.target_tokens.start + offset
        predecessor_token = predecessor_tokens[predecessor_index]
        target_token = target_tokens[target_index]
        if predecessor_token.text != target_token.text:
            continue
        if not (
            target_body[0] <= target_token.start
            and target_token.end <= target_body[1]
            and predecessor_body[0] <= predecessor_token.start
            and predecessor_token.end <= predecessor_body[1]
        ):
            continue
        if _WORD_RE.fullmatch(target_token.text):
            word_tokens += 1
    return word_tokens, non_whitespace_chars


def _result(
    *,
    verified: bool,
    predecessor_candidate: BoundaryCandidate | None = None,
    qualifying_predecessor_count: int = 0,
    exact_word_token_count: int = 0,
    exact_non_whitespace_char_count: int = 0,
    adjacent_equal_operation_indices: Sequence[int] = (),
    evidence: Sequence[str] = (),
) -> TokenPersistenceResult:
    return TokenPersistenceResult(
        verified=verified,
        predecessor_candidate=predecessor_candidate,
        qualifying_predecessor_count=qualifying_predecessor_count,
        exact_word_token_count=exact_word_token_count,
        exact_non_whitespace_char_count=exact_non_whitespace_char_count,
        adjacent_equal_operation_indices=tuple(adjacent_equal_operation_indices),
        evidence=tuple(evidence),
    )


def token_persistence_continuity(
    revision_diff: RevisionDiff,
    target_candidate: BoundaryCandidate,
    predecessor_candidates: Sequence[BoundaryCandidate],
    action_target_spans: Sequence[object],
) -> TokenPersistenceResult:
    """Verify strict exact-token continuity around every attributed edit span.

    This helper deliberately uses only raw offsets and Myers EQUAL operations.
    Signature, speaker, and revision-actor fields are not consulted. Structural
    whitespace outside the target body must be removed or handled by the caller
    before invoking this strict check.
    """

    target_raw = revision_diff.target.raw_text
    if target_raw is None or revision_diff.predecessor.raw_text is None:
        return _result(verified=False, evidence=("revision_text_unavailable",))
    spans, span_evidence = _positive_substantive_spans(action_target_spans, target_raw)
    if not spans:
        return _result(verified=False, evidence=span_evidence)
    target_body = (target_candidate.body_start, target_candidate.body_end)
    if not all(target_body[0] <= start and end <= target_body[1] for start, end in spans):
        return _result(verified=False, evidence=("target_span_outside_candidate_body",))

    edit_indices: list[tuple[int, tuple[int, int]]] = []
    for span in spans:
        matching = [
            index
            for index, operation in enumerate(revision_diff.operations)
            if operation.kind in {DiffOpKind.INSERT, DiffOpKind.REPLACE}
            and _overlap(
                (operation.target_chars.start, operation.target_chars.end), span
            )
        ]
        if not matching:
            return _result(verified=False, evidence=("target_span_has_no_insert_or_replace",))
        edit_indices.append((matching[0], span))

    qualifying: list[tuple[BoundaryCandidate, int, int, tuple[int, ...]]] = []
    for predecessor_candidate in predecessor_candidates:
        if not _clean_predecessor(predecessor_candidate):
            continue
        if (
            predecessor_candidate.indentation != target_candidate.indentation
            or predecessor_candidate.depth != target_candidate.depth
        ):
            continue
        metrics: list[tuple[int, int]] = []
        adjacent_indices: list[int] = []
        valid = True
        for edit_index, _span in edit_indices:
            neighbors = []
            if edit_index > 0:
                neighbors.append(edit_index - 1)
            if edit_index + 1 < len(revision_diff.operations):
                neighbors.append(edit_index + 1)
            matching_block: tuple[int, tuple[int, int]] | None = None
            for neighbor_index in neighbors:
                operation = revision_diff.operations[neighbor_index]
                if operation.kind is not DiffOpKind.EQUAL:
                    continue
                block_metrics = _equal_block_metrics(
                    revision_diff,
                    operation,
                    target_candidate,
                    predecessor_candidate,
                )
                if block_metrics is None:
                    continue
                if (
                    block_metrics[0] >= MIN_EXACT_WORD_TOKENS
                    and block_metrics[1] >= MIN_EXACT_NON_WHITESPACE_CHARS
                ):
                    matching_block = (neighbor_index, block_metrics)
                    break
            if matching_block is None:
                valid = False
                break
            adjacent_indices.append(matching_block[0])
            metrics.append(matching_block[1])
        if valid:
            qualifying.append(
                (
                    predecessor_candidate,
                    min(item[0] for item in metrics),
                    min(item[1] for item in metrics),
                    tuple(adjacent_indices),
                )
            )

    if len(qualifying) != 1:
        return _result(
            verified=False,
            qualifying_predecessor_count=len(qualifying),
            evidence=(
                "token_persistence_continuity",
                "no_unique_predecessor_candidate",
            ),
        )
    predecessor_candidate, word_tokens, non_whitespace_chars, adjacent_indices = qualifying[0]
    return _result(
        verified=True,
        predecessor_candidate=predecessor_candidate,
        qualifying_predecessor_count=1,
        exact_word_token_count=word_tokens,
        exact_non_whitespace_char_count=non_whitespace_chars,
        adjacent_equal_operation_indices=adjacent_indices,
        evidence=("token_persistence_continuity",),
    )


__all__ = [
    "MIN_EXACT_NON_WHITESPACE_CHARS",
    "MIN_EXACT_WORD_TOKENS",
    "TokenPersistenceResult",
    "token_persistence_continuity",
]
