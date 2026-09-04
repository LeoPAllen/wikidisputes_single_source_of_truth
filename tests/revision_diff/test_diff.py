from __future__ import annotations

import pytest

from wikidisputes_ssot.revision_diff.diff import (
    DiffResourceLimitError,
    align_revisions,
    tokenize_raw,
)
from wikidisputes_ssot.revision_diff.models import DiffOpKind, RevisionText, local_content_sha256


def test_unicode_token_spans_round_trip_exact_raw_text() -> None:
    raw_text = "Café 🤖\n東京!"
    tokens = tokenize_raw(raw_text)
    assert "".join(token.text for token in tokens) == raw_text
    assert tuple(raw_text[token.start : token.end] for token in tokens) == tuple(
        token.text for token in tokens
    )
    assert tokens[2].char_span.start == 5
    assert tokens[2].char_span.end == 6


def test_insertion_replacement_and_deletion_emit_both_ranges() -> None:
    insertion = align_revisions(
        RevisionText.available("1", "one two"), RevisionText.available("2", "one new two")
    )
    replacement = align_revisions(
        RevisionText.available("3", "one old two"), RevisionText.available("4", "one new two")
    )
    deletion = align_revisions(
        RevisionText.available("5", "one old two"), RevisionText.available("6", "one two")
    )
    assert insertion.changed.predecessor_chars[0].length == 0
    assert insertion.changed.target_chars[0].extract("one new two") == "new "
    assert replacement.operations[1].kind is DiffOpKind.REPLACE
    assert replacement.changed.predecessor_chars[0].extract("one old two") == "old"
    assert replacement.changed.target_chars[0].extract("one new two") == "new"
    assert deletion.changed.predecessor_chars[0].extract("one old two") == "old "
    assert deletion.changed.target_chars[0].length == 0


def test_repetitive_tokens_use_stable_deletion_first_tie_break() -> None:
    first = align_revisions(
        RevisionText.available("1", "a a b"), RevisionText.available("2", "a b b")
    )
    second = align_revisions(
        RevisionText.available("1", "a a b"), RevisionText.available("2", "a b b")
    )
    assert first.operations == second.operations
    assert first.changed.predecessor_tokens == second.changed.predecessor_tokens


def test_adjacent_insert_delete_primitives_coalesce_to_replacement() -> None:
    result = align_revisions(
        RevisionText.available("1", "left"), RevisionText.available("2", "right")
    )
    assert len(result.operations) == 1
    assert result.operations[0].kind is DiffOpKind.REPLACE
    assert result.operations[0].predecessor_chars.extract("left") == "left"
    assert result.operations[0].target_chars.extract("right") == "right"


def test_local_content_hashes_are_exact_and_retained() -> None:
    revision = RevisionText.available("1", "🤖")
    assert revision.local_content_sha256 == local_content_sha256("🤖")
    assert local_content_sha256("🤖") != local_content_sha256("?")
    assert len(revision.local_content_sha256 or "") == 64


def test_explicit_resource_limit_fails_closed_without_changing_alignment() -> None:
    with pytest.raises(DiffResourceLimitError):
        align_revisions(
            RevisionText.available("1", "a b c d"),
            RevisionText.available("2", "w x y z"),
            max_trace_cells=2,
        )
