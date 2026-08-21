from wikidisputes_ssot.core import _creation_anchor
from wikidisputes_ssot.full import (
    _source_identity_aliases,
    _source_logical_anchor,
)


def test_original_uses_current_id() -> None:
    row = {
        "wikidisputes_type_exact": "original",
        "wikidisputes_id_exact": "200.2.2",
        "wikidisputes_original_id_exact": "100.1.1",
        "source_row_uid": "row-1",
    }

    assert _creation_anchor(row) == (
        "200.2.2",
        "wikiconv_creation_id",
    )
    assert _source_logical_anchor(row) == "200.2.2"


def test_modified_source_occurrence_still_uses_current_id() -> None:
    row = {
        "wikidisputes_type_exact": "modification",
        "wikidisputes_id_exact": "503223900.27335.27335",
        "wikidisputes_original_id_exact": "503223030.27335.27335",
        "source_row_uid": "row-2",
    }

    assert _creation_anchor(row) == (
        "503223900.27335.27335",
        "source_current_id",
    )

    assert (
        _source_logical_anchor(row)
        == "503223900.27335.27335"
    )

    # original_id remains available to WikiConv lifecycle resolution.
    assert _source_identity_aliases(row) == [
        "503223900.27335.27335",
        "503223030.27335.27335",
    ]


def test_same_current_id_cannot_split_when_original_id_missing() -> None:
    with_original = {
        "wikidisputes_type_exact": "modification",
        "wikidisputes_id_exact": "503223900.27335.27335",
        "wikidisputes_original_id_exact": "503223030.27335.27335",
        "source_row_uid": "row-a",
    }

    without_original = {
        "wikidisputes_type_exact": "modification",
        "wikidisputes_id_exact": "503223900.27335.27335",
        "wikidisputes_original_id_exact": None,
        "source_row_uid": "row-b",
    }

    assert (
        _source_logical_anchor(with_original)
        == _source_logical_anchor(without_original)
        == "503223900.27335.27335"
    )
