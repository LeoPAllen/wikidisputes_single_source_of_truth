from wikidisputes_ssot.full import (
    _normalize_wikidisputes_creation_timestamp,
    _repair_wikiconv_creation_timestamp,
    _resolve_creation_timestamp,
)


def test_winter_eastern_artifact_repair() -> None:
    repaired, status = _repair_wikiconv_creation_timestamp("2007-01-12T07:22:47+00:00")

    assert repaired == "2007-01-12T02:22:47+00:00"
    assert status == ("wikiconv_creation_time_corrected_eastern_artifact")


def test_summer_eastern_artifact_repair() -> None:
    repaired, status = _repair_wikiconv_creation_timestamp("2012-07-20T04:59:12+00:00")

    assert repaired == "2012-07-20T00:59:12+00:00"
    assert status == ("wikiconv_creation_time_corrected_eastern_artifact")


def test_missing_timestamp_remains_missing() -> None:
    repaired, status = _repair_wikiconv_creation_timestamp(None)

    assert repaired is None
    assert status == "wikiconv_creation_time_unavailable"


def test_wikidisputes_winter_london_wall_time_is_utc() -> None:
    normalized, status = _normalize_wikidisputes_creation_timestamp("2003-01-15T12:00:00Z")
    assert normalized == "2003-01-15T12:00:00+00:00"
    assert status == "wikidisputes_creation_time_normalized_europe_london"


def test_wikidisputes_bst_wall_time_is_shifted_to_utc() -> None:
    normalized, status = _normalize_wikidisputes_creation_timestamp("2005-05-09T17:35:16Z")
    assert normalized == "2005-05-09T16:35:16+00:00"
    assert status == "wikidisputes_creation_time_normalized_europe_london"


def test_wikidisputes_ambiguous_dst_fold_is_unresolved_without_stronger_evidence() -> None:
    normalized, status = _normalize_wikidisputes_creation_timestamp("2020-10-25T01:30:00Z")
    assert normalized is None
    assert status == "wikidisputes_creation_time_ambiguous_dst_fold"


def test_wikidisputes_ambiguous_dst_fold_uses_matching_stronger_evidence() -> None:
    normalized, status = _normalize_wikidisputes_creation_timestamp(
        "2020-10-25T01:30:00Z", preferred_utc="2020-10-25T00:30:00Z"
    )
    assert normalized == "2020-10-25T00:30:00+00:00"
    assert status == "wikidisputes_creation_time_ambiguous_fold_resolved_by_stronger_evidence"


def test_modification_or_restoration_action_time_is_never_creation_time() -> None:
    created_at, status, raw = _resolve_creation_timestamp(
        creation_revision_id=100,
        creation_action=None,
        original_source={
            "wikidisputes_type_exact": "restoration",
            "wikidisputes_time": "2020-01-02T03:04:05Z",
        },
        revision_timestamp_evidence={},
    )
    assert (created_at, status, raw) == (None, "creation_timestamp_unresolved", None)
