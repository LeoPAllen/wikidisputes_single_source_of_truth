from __future__ import annotations

from dataclasses import replace

from wikidisputes_ssot.revision_diff.boundaries import extract_comment_candidates
from wikidisputes_ssot.revision_diff.diff import align_revisions
from wikidisputes_ssot.revision_diff.models import RevisionText
from wikidisputes_ssot.revision_diff.token_persistence import token_persistence_continuity


def _signed(body: str, user: str = "Alice") -> str:
    return f"{body} -- [[User:{user}]] 12:34, 1 January 2020 (UTC)"


def _candidate_pair(predecessor_body: str, target_body: str):
    predecessor = _signed(predecessor_body)
    target = _signed(target_body)
    diff = align_revisions(
        RevisionText.available("1", predecessor),
        RevisionText.available("2", target),
    )
    return (
        diff,
        extract_comment_candidates(predecessor)[0],
        extract_comment_candidates(target)[0],
    )


def test_unique_predecessor_with_adjacent_exact_block_passes() -> None:
    diff, predecessor_candidate, target_candidate = _candidate_pair(
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet old lambda.",
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet new lambda.",
    )
    span = diff.changed.target_chars[0]
    result = token_persistence_continuity(
        diff,
        target_candidate,
        [predecessor_candidate],
        [span],
    )

    assert result.verified is True
    assert result.predecessor_candidate is predecessor_candidate
    assert result.qualifying_predecessor_count == 1
    assert result.exact_word_token_count >= 10
    assert result.exact_non_whitespace_char_count >= 40
    assert result.evidence == ("token_persistence_continuity",)


def test_repeated_qualifying_predecessor_candidates_fail_closed() -> None:
    diff, predecessor_candidate, target_candidate = _candidate_pair(
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet old lambda.",
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet new lambda.",
    )
    duplicate = replace(predecessor_candidate, candidate_uid="duplicate-predecessor")
    result = token_persistence_continuity(
        diff,
        target_candidate,
        [predecessor_candidate, duplicate],
        [diff.changed.target_chars[0]],
    )

    assert result.verified is False
    assert result.predecessor_candidate is None
    assert result.qualifying_predecessor_count == 2


def test_adjacent_block_below_both_exact_thresholds_fails() -> None:
    diff, predecessor_candidate, target_candidate = _candidate_pair(
        "Alpha bravo old lambda.",
        "Alpha bravo new lambda.",
    )
    result = token_persistence_continuity(
        diff,
        target_candidate,
        [predecessor_candidate],
        [diff.changed.target_chars[0]],
    )

    assert result.verified is False
    assert result.qualifying_predecessor_count == 0


def test_distant_long_equal_block_does_not_substitute_for_adjacent_block() -> None:
    prefix = "Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
    predecessor_body = f"{prefix} old-one q old-two tail"
    target_body = f"{prefix} new-one q new-two tail"
    diff, predecessor_candidate, target_candidate = _candidate_pair(
        predecessor_body,
        target_body,
    )
    # The second replacement is bordered only by the one-token ``q`` and the
    # short terminal block; the long prefix is intentionally non-adjacent.
    result = token_persistence_continuity(
        diff,
        target_candidate,
        [predecessor_candidate],
        [diff.changed.target_chars[1]],
    )

    assert result.verified is False
    assert result.qualifying_predecessor_count == 0


def test_non_body_action_span_is_rejected_even_with_a_good_equal_block() -> None:
    diff, predecessor_candidate, target_candidate = _candidate_pair(
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet old lambda.",
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet new lambda.",
    )
    assert target_candidate.signature_start is not None
    outside = (
        target_candidate.signature_start,
        target_candidate.signature_start + len("--"),
    )
    result = token_persistence_continuity(
        diff,
        target_candidate,
        [predecessor_candidate],
        [outside],
    )

    assert result.verified is False
    assert result.evidence == ("target_span_outside_candidate_body",)
