"""Deterministic token alignment for Method-B revision recovery."""

from __future__ import annotations

import re
from typing import Final

from .models import (
    ChangedRanges,
    DiffCandidate,
    DiffEvidence,
    DiffOpKind,
    DiffOperation,
    MethodBAction,
    RawMappedToken,
    RecoveryDecision,
    RevisionDiff,
    RevisionText,
    TextSpan,
    TokenRange,
)


_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)
DEFAULT_MAX_TRACE_CELLS: Final[int] = 2_000_000


class DiffResourceLimitError(RuntimeError):
    """The exact alignment exceeded an explicit operational memory bound."""


def tokenize_raw(raw_text: str) -> tuple[RawMappedToken, ...]:
    """Tokenize without normalization, preserving every code point exactly once."""

    return tuple(
        RawMappedToken(match.group(0), TextSpan(match.start(), match.end()))
        for match in _TOKEN_PATTERN.finditer(raw_text)
    )


def align_revisions(
    predecessor: RevisionText,
    target: RevisionText,
    *,
    max_trace_cells: int = DEFAULT_MAX_TRACE_CELLS,
) -> RevisionDiff:
    """Align available revision texts using exact Myers shortest-edit alignment.

    At an equal-path branch, deletion is chosen before insertion. This fixed
    rule gives repeated token sequences a stable alignment without heuristics.
    The trace limit is an explicit operational guard: exceeding it produces an
    auditable unresolved result upstream rather than changing the algorithm.
    """

    if predecessor.raw_text is None or target.raw_text is None:
        raise ValueError("both revision texts must be available for alignment")
    predecessor_tokens = tokenize_raw(predecessor.raw_text)
    target_tokens = tokenize_raw(target.raw_text)
    primitives = _myers_primitives(
        predecessor_tokens, target_tokens, max_trace_cells=max_trace_cells
    )
    operations = _coalesce_operations(
        primitives,
        predecessor_tokens,
        target_tokens,
        len(predecessor.raw_text),
        len(target.raw_text),
    )
    changed_operations = tuple(
        operation for operation in operations if operation.kind is not DiffOpKind.EQUAL
    )
    changed = ChangedRanges(
        predecessor_tokens=tuple(operation.predecessor_tokens for operation in changed_operations),
        target_tokens=tuple(operation.target_tokens for operation in changed_operations),
        predecessor_chars=tuple(operation.predecessor_chars for operation in changed_operations),
        target_chars=tuple(operation.target_chars for operation in changed_operations),
    )
    return RevisionDiff(predecessor, target, predecessor_tokens, target_tokens, operations, changed)


def candidates_from_diff(revision_diff: RevisionDiff) -> tuple[DiffCandidate, ...]:
    """Return one immutable recovery candidate for each changed operation."""

    predecessor_text = revision_diff.predecessor.raw_text
    target_text = revision_diff.target.raw_text
    if predecessor_text is None or target_text is None:
        raise ValueError("revision diff must retain both available raw texts")
    candidates: list[DiffCandidate] = []
    for index, operation in enumerate(revision_diff.operations):
        if operation.kind is DiffOpKind.EQUAL:
            continue
        action = MethodBAction(operation.kind.value)
        candidates.append(
            DiffCandidate(
                action=action,
                operation_index=index,
                predecessor_text=operation.predecessor_chars.extract(predecessor_text),
                target_text=operation.target_chars.extract(target_text),
                predecessor_chars=operation.predecessor_chars,
                target_chars=operation.target_chars,
            )
        )
    return tuple(candidates)


def evidence_from_diff(revision_diff: RevisionDiff) -> DiffEvidence:
    """Return the revision identity and local content state for a diff result."""

    predecessor = revision_diff.predecessor
    target = revision_diff.target
    return DiffEvidence(
        predecessor_revision_id=predecessor.revision_id,
        target_revision_id=target.revision_id,
        predecessor_api_sha1=predecessor.api_sha1,
        target_api_sha1=target.api_sha1,
        predecessor_local_content_sha256=predecessor.local_content_sha256,
        target_local_content_sha256=target.local_content_sha256,
        operation_count=len(revision_diff.operations),
    )


def decision_from_diff(revision_diff: RevisionDiff) -> RecoveryDecision:
    """Make a conservative Method-B decision from a complete alignment."""

    candidates = candidates_from_diff(revision_diff)
    evidence = evidence_from_diff(revision_diff)
    if not candidates:
        return RecoveryDecision(
            MethodBAction.NO_CHANGE, None, evidence, "texts are identical"
        )
    if len(candidates) == 1:
        return RecoveryDecision(
            candidates[0].action, candidates[0], evidence, "single changed range"
        )
    return RecoveryDecision(
        MethodBAction.UNRESOLVED,
        None,
        evidence,
        "multiple changed ranges require downstream review",
    )


def _myers_primitives(
    predecessor: tuple[RawMappedToken, ...],
    target: tuple[RawMappedToken, ...],
    *,
    max_trace_cells: int,
) -> tuple[DiffOpKind, ...]:
    """Return an exact EQUAL/DELETE/INSERT script with no junk heuristic."""

    if max_trace_cells < 1:
        raise ValueError("max_trace_cells must be positive")
    before = tuple(token.text for token in predecessor)
    after = tuple(token.text for token in target)
    before_count = len(before)
    after_count = len(after)
    if not before_count:
        return (DiffOpKind.INSERT,) * after_count
    if not after_count:
        return (DiffOpKind.DELETE,) * before_count

    frontier: dict[int, int] = {1: 0}
    trace: list[dict[int, int]] = []
    trace_cells = 0
    final_distance: int | None = None
    for distance in range(before_count + after_count + 1):
        trace.append(frontier.copy())
        trace_cells += len(frontier)
        if trace_cells > max_trace_cells:
            raise DiffResourceLimitError(
                "exact Myers trace exceeded max_trace_cells=" f"{max_trace_cells}"
            )
        for diagonal in range(-distance, distance + 1, 2):
            # Strict '<' makes an equal-path branch choose deletion.
            if diagonal == -distance or (
                diagonal != distance
                and frontier.get(diagonal - 1, -1) < frontier.get(diagonal + 1, -1)
            ):
                x = frontier.get(diagonal + 1, 0)
            else:
                x = frontier.get(diagonal - 1, 0) + 1
            y = x - diagonal
            while x < before_count and y < after_count and before[x] == after[y]:
                x += 1
                y += 1
            frontier[diagonal] = x
            if x >= before_count and y >= after_count:
                final_distance = distance
                break
        if final_distance is not None:
            break
    if final_distance is None:
        raise RuntimeError("Myers alignment failed to find an edit script")

    reverse: list[DiffOpKind] = []
    x = before_count
    y = after_count
    for distance in range(final_distance, 0, -1):
        previous_frontier = trace[distance]
        diagonal = x - y
        if diagonal == -distance or (
            diagonal != distance
            and previous_frontier.get(diagonal - 1, -1)
            < previous_frontier.get(diagonal + 1, -1)
        ):
            previous_diagonal = diagonal + 1
            primitive = DiffOpKind.INSERT
        else:
            previous_diagonal = diagonal - 1
            primitive = DiffOpKind.DELETE
        previous_x = previous_frontier[previous_diagonal]
        previous_y = previous_x - previous_diagonal
        while x > previous_x and y > previous_y:
            reverse.append(DiffOpKind.EQUAL)
            x -= 1
            y -= 1
        reverse.append(primitive)
        if primitive is DiffOpKind.DELETE:
            x -= 1
        else:
            y -= 1
    while x > 0 and y > 0:
        reverse.append(DiffOpKind.EQUAL)
        x -= 1
        y -= 1
    reverse.extend(DiffOpKind.DELETE for _ in range(x))
    reverse.extend(DiffOpKind.INSERT for _ in range(y))
    reverse.reverse()
    return tuple(reverse)


def _coalesce_operations(
    primitives: tuple[DiffOpKind, ...],
    predecessor_tokens: tuple[RawMappedToken, ...],
    target_tokens: tuple[RawMappedToken, ...],
    predecessor_length: int,
    target_length: int,
) -> tuple[DiffOperation, ...]:
    output: list[DiffOperation] = []
    predecessor_index = 0
    target_index = 0
    run_start_predecessor = 0
    run_start_target = 0
    run: list[DiffOpKind] = []

    def flush() -> None:
        if not run:
            return
        predecessor_range = TokenRange(run_start_predecessor, predecessor_index)
        target_range = TokenRange(run_start_target, target_index)
        if all(kind is DiffOpKind.EQUAL for kind in run):
            kind = DiffOpKind.EQUAL
        elif predecessor_range.length == 0:
            kind = DiffOpKind.INSERT
        elif target_range.length == 0:
            kind = DiffOpKind.DELETE
        else:
            kind = DiffOpKind.REPLACE
        output.append(
            DiffOperation(
                kind,
                predecessor_range,
                target_range,
                _character_range(predecessor_tokens, predecessor_range, predecessor_length),
                _character_range(target_tokens, target_range, target_length),
            )
        )
        run.clear()

    for primitive in primitives:
        if run and ((primitive is DiffOpKind.EQUAL) != (run[0] is DiffOpKind.EQUAL)):
            flush()
            run_start_predecessor = predecessor_index
            run_start_target = target_index
        if not run:
            run_start_predecessor = predecessor_index
            run_start_target = target_index
        run.append(primitive)
        if primitive is not DiffOpKind.INSERT:
            predecessor_index += 1
        if primitive is not DiffOpKind.DELETE:
            target_index += 1
    flush()
    return tuple(output)


def _character_range(
    tokens: tuple[RawMappedToken, ...], token_range: TokenRange, raw_length: int
) -> TextSpan:
    if token_range.length:
        return TextSpan(tokens[token_range.start].start, tokens[token_range.end - 1].end)
    if token_range.start < len(tokens):
        boundary = tokens[token_range.start].start
    elif tokens:
        boundary = tokens[-1].end
    else:
        boundary = raw_length
    return TextSpan(boundary, boundary)
