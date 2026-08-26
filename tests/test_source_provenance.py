import pytest

from wikidisputes_ssot.source_provenance import (
    check_source_text_provenance,
    source_text_sha256,
)


def test_exact_canonical_source_text_passes() -> None:
    result = check_source_text_provenance(
        [{"source_row_uid": "row-1", "source_text": "Exact [[wiki]] text"}],
        {"row-1": "Exact [[wiki]] text"},
    )
    assert result.ok
    assert result.checked_rows == 1


def test_downstream_annotation_text_cannot_replace_source_target() -> None:
    result = check_source_text_provenance(
        [{"source_row_uid": "row-1", "source_text": "target plus adjacent comment"}],
        {"row-1": "target"},
    )
    assert not result.ok
    assert result.text_mismatches == ("row-1",)
    with pytest.raises(RuntimeError, match="text_mismatches"):
        result.require_ok(label="polluted annotation export")


def test_missing_and_duplicate_occurrences_are_rejected() -> None:
    rows = [
        {"source_row_uid": "missing", "source_text": "x"},
        {"source_row_uid": "missing", "source_text": "x"},
    ]
    result = check_source_text_provenance(rows, {})
    assert result.missing_source_rows == ("missing",)
    assert result.duplicate_source_rows == ("missing",)


def test_source_hash_is_stable_and_markup_sensitive() -> None:
    assert source_text_sha256("[[A|B]]") == source_text_sha256("[[A|B]]")
    assert source_text_sha256("[[A|B]]") != source_text_sha256("B")
