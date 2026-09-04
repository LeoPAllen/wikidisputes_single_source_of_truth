"""Conservative structural-candidate localization for revision diffs.

Whole-page parsing remains useful for finding defensible comment boundaries, but
revision-global assignment must only see comments the changed target spans can
actually support.  This module deliberately has no radius or proximity search:
it admits direct overlap plus whitespace-only structural boundary adjacency.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .boundaries import BoundaryCandidate

Span = tuple[int, int]


@dataclass(frozen=True)
class LocalizationMatch:
    """One retained candidate/span relationship and its audit reasons."""

    candidate_uid: str
    target_span: Span
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LocalizedCandidates:
    """Deterministic candidate subset with complete localization evidence."""

    candidates: tuple[BoundaryCandidate, ...]
    matches: tuple[LocalizationMatch, ...]
    whole_page_candidate_count: int
    localized_candidate_count: int

    def reasons_for(self, candidate_uid: str, target_span: Span) -> tuple[str, ...]:
        """Return reasons for one candidate/span pair, or an empty tuple."""
        for match in self.matches:
            if match.candidate_uid == candidate_uid and match.target_span == target_span:
                return match.reasons
        return ()


def _normalise_spans(spans: Iterable[Span]) -> tuple[Span, ...]:
    """Validate and sort spans without silently clipping archival offsets."""
    normalized: set[Span] = set()
    for start, end in spans:
        if start < 0 or end < start:
            raise ValueError(f"invalid target span: {(start, end)!r}")
        normalized.add((int(start), int(end)))
    return tuple(sorted(normalized))


def _overlaps_or_contains(candidate: BoundaryCandidate, span: Span) -> bool:
    """Return direct interior/positive-width structural overlap."""
    start, end = span
    if start == end:
        return candidate.start < start < candidate.end
    return candidate.start < end and start < candidate.end


def _span_is_whitespace(page_raw: str, span: Span) -> bool:
    start, end = span
    return start < end <= len(page_raw) and page_raw[start:end].isspace()


def _is_immediate_whitespace_neighbor(
    page_raw: str,
    candidates: Sequence[BoundaryCandidate],
    index: int,
    span: Span,
) -> bool:
    """Whether a whitespace-only span borders this candidate with none between.

    This is intentionally structural rather than distance based.  A long blank
    region is still admissible only for its nearest signed comment on either
    side; no candidate is admitted merely for being *near* a hunk.
    """
    start, end = span
    candidate = candidates[index]
    if start == end:
        point = start
        if point == candidate.start or point == candidate.end:
            return True
        if point < candidate.start:
            if index and candidates[index - 1].end > point:
                return False
            return not page_raw[point : candidate.start].strip()
        if point > candidate.end:
            if index + 1 < len(candidates) and candidates[index + 1].start < point:
                return False
            return not page_raw[candidate.end : point].strip()
        return False
    if not _span_is_whitespace(page_raw, span):
        return False
    if end <= candidate.start:
        if index and candidates[index - 1].end > start:
            return False
        bridge = page_raw[end : candidate.start]
        return not bridge or bridge.isspace()
    if start >= candidate.end:
        if index + 1 < len(candidates) and candidates[index + 1].start < end:
            return False
        bridge = page_raw[candidate.end : start]
        return not bridge or bridge.isspace()
    return False


def _overlap_reasons(page_raw: str, candidate: BoundaryCandidate, span: Span) -> tuple[str, ...]:
    if not _overlaps_or_contains(candidate, span):
        return ()
    reasons = ["changed_target_span_overlaps_or_contains_candidate"]
    overlap_start = max(span[0], candidate.start)
    overlap_end = min(span[1], candidate.end)
    outside = page_raw[span[0] : overlap_start] + page_raw[overlap_end : span[1]]
    if outside and not outside.strip():
        reasons.append("changed_target_span_differs_only_by_structural_boundary_whitespace")
    return tuple(reasons)


def localize_candidates(
    page_raw: str,
    whole_page_candidates: Sequence[BoundaryCandidate],
    changed_target_spans: Iterable[Span],
) -> LocalizedCandidates:
    """Return only candidates defensibly associated with target-side changes.

    A candidate is retained if a changed span overlaps or contains it.  A
    whitespace-only hunk in the structural gap between comments retains only
    the immediate comment(s) bordering that gap.  The result order follows raw
    offsets, never input/action ordering.
    """
    candidates = tuple(
        sorted(
            whole_page_candidates,
            key=lambda item: (item.start, item.end, item.candidate_uid),
        )
    )
    spans = _normalise_spans(changed_target_spans)
    if any(end > len(page_raw) for _, end in spans):
        raise ValueError("target span exceeds page raw text")

    matches: list[LocalizationMatch] = []
    retained_uids: set[str] = set()
    for span in spans:
        for index, candidate in enumerate(candidates):
            reasons: list[str] = []
            if direct_reasons := _overlap_reasons(page_raw, candidate, span):
                reasons.extend(direct_reasons)
            elif _is_immediate_whitespace_neighbor(page_raw, candidates, index, span):
                reasons.extend(
                    (
                        "changed_target_span_structural_boundary_whitespace",
                        "immediate_structural_neighbor_at_ambiguous_boundary",
                    )
                )
            if reasons:
                retained_uids.add(candidate.candidate_uid)
                matches.append(LocalizationMatch(candidate.candidate_uid, span, tuple(reasons)))

    localized = tuple(
        candidate for candidate in candidates if candidate.candidate_uid in retained_uids
    )
    return LocalizedCandidates(
        candidates=localized,
        matches=tuple(matches),
        whole_page_candidate_count=len(candidates),
        localized_candidate_count=len(localized),
    )
