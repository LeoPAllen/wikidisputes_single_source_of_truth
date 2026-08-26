"""Revision-local, evidence-first action to comment-candidate assignment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .boundaries import BoundaryCandidate


@dataclass(frozen=True)
class AssignmentConfig:
    """Ambiguity policy, not a calibrated confidence or safety score."""

    ambiguity_tolerance: int = 1
    max_actions_for_exhaustive_search: int = 10
    max_candidates_for_exhaustive_search: int = 30
    max_edges_for_exhaustive_search: int = 200
    max_search_states: int = 100_000


@dataclass(frozen=True)
class AssignmentEdge:
    action_uid: str
    candidate_uid: str
    evidence: tuple[str, ...]
    rank: tuple[int, ...]


@dataclass(frozen=True)
class AssignmentResult:
    action_uid: str
    candidate_uid: str | None
    status: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def _get(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _span(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return int(value[0]), int(value[1])
    start, end = _get(value, "start"), _get(value, "end")
    return (int(start), int(end)) if start is not None and end is not None else None


def _overlaps(left: tuple[int, int], right: tuple[int, int]) -> bool:
    if left[0] == left[1]:
        return right[0] <= left[0] <= right[1]
    if right[0] == right[1]:
        return left[0] <= right[0] <= left[1]
    return left[0] < right[1] and right[0] < left[1]


def _words(value: Any) -> set[str]:
    return set(re.findall(r"\w+", str(value or "").casefold()))


def _action_uid(action: Any, index: int) -> str:
    return str(_get(action, "action_uid", "id", "action_id", "source_row_uid") or f"action:{index}")


def _changed_target_spans(action: Any) -> list[tuple[int, int]]:
    values = _get(action, "changed_ranges", "changed_spans", "hunks", "changed_range") or ()
    if not isinstance(values, (list, tuple)):
        values = (values,)
    output = []
    for value in values:
        span = _span(_get(value, "target_span", "target", "after_span", "new_span") or value)
        if span:
            output.append(span)
    return output


def build_assignment_edges(
    actions: Sequence[Any], candidates: Sequence[BoundaryCandidate]
) -> list[AssignmentEdge]:
    edges: list[AssignmentEdge] = []
    for index, action in enumerate(actions):
        uid = _action_uid(action, index)
        action_type = str(_get(action, "action_type", "lifecycle") or "").casefold()
        source = _get(action, "informative_text", "text", "source_text", "body")
        logical = _get(action, "logical_utterance_uid", "logical_id")
        actor = _get(action, "speaker", "actor", "user", "author")
        offset = _get(action, "offset_hint", "target_offset", "character_start")
        spans = _changed_target_spans(action)
        for candidate in candidates:
            evidence: list[str] = []
            changed = any(_overlaps((candidate.start, candidate.end), span) for span in spans)
            if changed:
                evidence.append("changed_target_span_overlaps_candidate")
            if offset is not None and candidate.start <= int(offset) < candidate.end:
                evidence.append("offset_hint_inside_candidate")
            source_words = _words(source)
            candidate_words = _words(candidate.body_wikitext)
            if source_words and len(source_words & candidate_words) >= min(3, len(source_words)):
                evidence.append("informative_text_tokens_observed")
            if (
                actor
                and candidate.signature_user_target
                and str(actor).casefold() == candidate.signature_user_target.casefold()
            ):
                evidence.append("actor_matches_signature_target")
            candidate_logical = _get(candidate, "logical_utterance_uid", "logical_id")
            if logical and candidate_logical and str(logical) == str(candidate_logical):
                evidence.append("logical_identity_matches")
            if action_type in {"creation", "modification", "restoration"} and changed:
                evidence.append("lifecycle_compatible_with_target_change")
            # Lexicographic evidence: no empirical score is produced or exposed.
            rank = (
                int("changed_target_span_overlaps_candidate" in evidence),
                int("logical_identity_matches" in evidence),
                int("lifecycle_compatible_with_target_change" in evidence),
                int("informative_text_tokens_observed" in evidence),
                int("offset_hint_inside_candidate" in evidence),
                int("actor_matches_signature_target" in evidence),
                1,
            )
            if any(rank):
                edges.append(AssignmentEdge(uid, candidate.candidate_uid, tuple(evidence), rank))
    return edges


def assign_actions_to_candidates(
    actions: Sequence[Any],
    candidates: Sequence[BoundaryCandidate],
    *,
    config: AssignmentConfig = AssignmentConfig(),
) -> list[AssignmentResult]:
    """Globally assign each candidate once; mark plausible ties ambiguous.

    The small revision-local search is exhaustive.  Larger inputs remain
    conservative by returning ambiguity instead of a greedy, order-dependent
    allocation.
    """
    edges = build_assignment_edges(actions, candidates)
    by_action: dict[str, list[AssignmentEdge]] = {}
    for edge in edges:
        by_action.setdefault(edge.action_uid, []).append(edge)
    action_ids = [_action_uid(action, i) for i, action in enumerate(actions)]
    if (
        len(action_ids) > config.max_actions_for_exhaustive_search
        or len(candidates) > config.max_candidates_for_exhaustive_search
        or len(edges) > config.max_edges_for_exhaustive_search
    ):
        return [
            AssignmentResult(
                uid,
                None,
                "ambiguous",
                (),
                ("revision_too_large_for_safe_global_assignment",),
            )
            for uid in action_ids
        ]

    # Each vector component is accumulated separately, preserving lexicographic
    # precedence rather than treating unrelated evidence as interchangeable.
    possibilities: list[tuple[tuple[int, ...], dict[str, AssignmentEdge]]] = []
    visited_states = 0

    class SearchLimitReached(RuntimeError):
        pass

    def visit(
        position: int,
        used: set[str],
        chosen: dict[str, AssignmentEdge],
        total: tuple[int, ...],
    ) -> None:
        nonlocal visited_states
        visited_states += 1
        if visited_states > config.max_search_states:
            raise SearchLimitReached
        if position == len(action_ids):
            possibilities.append((total, chosen.copy()))
            return
        uid = action_ids[position]
        visit(position + 1, used, chosen, total)
        for edge in by_action.get(uid, ()):
            if edge.candidate_uid not in used:
                chosen[uid] = edge
                updated = tuple(a + b for a, b in zip(total, edge.rank))
                visit(position + 1, used | {edge.candidate_uid}, chosen, updated)
                del chosen[uid]
    try:
        visit(0, set(), {}, (0, 0, 0, 0, 0, 0, 0))
    except SearchLimitReached:
        return [
            AssignmentResult(uid, None, "ambiguous", (), ("global_assignment_search_limit",))
            for uid in action_ids
        ]
    best = max(score for score, _ in possibilities)
    # A near-tie is relevant only when the two strongest, target-independent
    # components agree; weaker hints may not manufacture uniqueness.
    optimal = [
        chosen
        for score, chosen in possibilities
        if score[:2] == best[:2]
        and sum(best[index] - score[index] for index in range(2, len(best)))
        <= config.ambiguity_tolerance
    ]
    results: list[AssignmentResult] = []
    for uid in action_ids:
        choices = {chosen[uid].candidate_uid if uid in chosen else None for chosen in optimal}
        if len(choices) != 1:
            results.append(
                AssignmentResult(uid, None, "ambiguous", (), ("equal_global_assignments",))
            )
            continue
        candidate_uid = next(iter(choices))
        if candidate_uid is None:
            status = "unmatched" if not by_action.get(uid) else "ambiguous"
            warnings = ("no_unique_global_assignment",) if status == "ambiguous" else ()
            results.append(AssignmentResult(uid, None, status, (), warnings))
            continue
        edge = next(chosen[uid] for chosen in optimal if uid in chosen)
        # A local equal-best edge is ambiguity even if iteration order happened
        # to select it; global uniqueness above handles candidate collisions.
        local = sorted(by_action.get(uid, ()), key=lambda item: item.rank, reverse=True)
        if len(local) > 1 and local[0].rank == local[1].rank:
            results.append(
                AssignmentResult(
                    uid, None, "ambiguous", edge.evidence, ("equal_local_evidence",)
                )
            )
        else:
            results.append(AssignmentResult(uid, candidate_uid, "assigned", edge.evidence))
    return results


assign_revision_actions = assign_actions_to_candidates
