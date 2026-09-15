import pytest

from wikidisputes_ssot.core import _creation_anchor
from wikidisputes_ssot.full import (
    IdentityConflictError,
    _reconcile_source_identities,
    _source_identity_aliases,
    _source_logical_anchor,
)


def test_original_creation_uses_current_id() -> None:
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


def test_modified_occurrence_anchors_to_authoritative_original_id() -> None:
    row = {
        "wikidisputes_type_exact": "modification",
        "wikidisputes_id_exact": "503223900.27335.27335",
        "wikidisputes_original_id_exact": "503223030.27335.27335",
        "source_row_uid": "row-2",
    }

    assert _creation_anchor(row) == (
        "503223030.27335.27335",
        "wikiconv_original_id",
    )

    assert _source_logical_anchor(row) == "503223030.27335.27335"

    # original_id remains available to WikiConv lifecycle resolution.
    assert _source_identity_aliases(row) == [
        "503223900.27335.27335",
        "503223030.27335.27335",
    ]


def test_exact_current_id_alias_links_occurrences_when_original_id_is_missing() -> None:
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

    # The rooted occurrence contributes the authoritative original ID. The
    # unrooted occurrence contributes the exact current action ID, which is
    # also present in the rooted row's aliases. Pipeline reconciliation must
    # therefore propagate the unique root across these equivalent rows.
    assert _source_logical_anchor(with_original) == "503223030.27335.27335"
    assert _source_logical_anchor(without_original) == "503223900.27335.27335"
    assert set(_source_identity_aliases(with_original)) & set(
        _source_identity_aliases(without_original)
    ) == {"503223900.27335.27335"}


def test_exact_action_alias_propagates_unique_original_root() -> None:
    rooted = {
        "wikidisputes_type_exact": "modification",
        "wikidisputes_id_exact": "503223900.27335.27335",
        "wikidisputes_original_id_exact": "503223030.27335.27335",
        "source_row_uid": "rooted-row",
    }
    unrooted = {
        "wikidisputes_type_exact": "modification",
        "wikidisputes_id_exact": "503223900.27335.27335",
        "wikidisputes_original_id_exact": None,
        "source_row_uid": "unrooted-row",
    }

    result = _reconcile_source_identities([rooted, unrooted])

    assert (
        result["row_to_logical_uid"]["rooted-row"] == result["row_to_logical_uid"]["unrooted-row"]
    )
    assert result["row_to_logical_uid"]["rooted-row"] == "wikiconv:503223030.27335.27335"
    assert result["resolved_conflicts"] == []


def test_conflicting_authoritative_roots_for_shared_action_alias_fail_closed() -> None:
    first = {
        "wikidisputes_type_exact": "modification",
        "wikidisputes_id_exact": "503223900.27335.27335",
        "wikidisputes_original_id_exact": "503223030.27335.27335",
        "source_row_uid": "first-root",
    }
    second = {
        "wikidisputes_type_exact": "modification",
        "wikidisputes_id_exact": "503223900.27335.27335",
        "wikidisputes_original_id_exact": "503222999.27335.27335",
        "source_row_uid": "second-root",
    }

    with pytest.raises(IdentityConflictError, match="503223900\\.27335\\.27335"):
        _reconcile_source_identities([first, second])
