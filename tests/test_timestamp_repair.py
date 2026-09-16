import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from wikidisputes_ssot.full import (
    _load_mediawiki_revision_timestamps,
    _normalize_wikidisputes_creation_timestamp,
    _repair_wikiconv_creation_timestamp,
    _resolve_creation_timestamp,
    _resolve_creation_timestamp_evidence,
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


def test_invalid_wikiconv_creation_falls_through_to_authoritative_source() -> None:
    evidence = _resolve_creation_timestamp_evidence(
        creation_revision_id=100,
        creation_action={"action_type": "creation", "timestamp": "invalid"},
        original_source={
            "wikidisputes_type_exact": "original",
            "wikidisputes_id_exact": "root-1",
            "wikidisputes_time": "2005-05-09T17:35:16Z",
        },
        revision_timestamp_evidence={},
    )
    assert evidence["created_at_status"] == "wikidisputes_creation_time_normalized_europe_london"
    assert evidence["created_at_utc"] == "2005-05-09T16:35:16+00:00"
    assert [attempt["tier"] for attempt in evidence["creation_evidence_attempts"]] == [
        "mediawiki_revision_timestamp",
        "wikiconv_creation_lifecycle",
    ]


def test_conflicting_or_non_authoritative_source_cannot_supply_creation_time() -> None:
    evidence = _resolve_creation_timestamp_evidence(
        creation_revision_id=None,
        creation_action=None,
        original_source={
            "wikidisputes_type_exact": "original",
            "wikidisputes_id_exact": "root-1",
            "wikidisputes_time": "2005-05-09T17:35:16Z",
        },
        revision_timestamp_evidence={},
        source_creation_authoritative=False,
    )
    assert evidence["created_at_utc"] is None
    assert evidence["created_at_status"] == "wikidisputes_creation_root_ambiguous"


def test_lifecycle_event_time_stays_separate_from_creation_time() -> None:
    from wikidisputes_ssot.full import _normalize_lifecycle_event_time

    normalized, status, timezone = _normalize_lifecycle_event_time(
        1342774752, source="wikiconv_nested_lifecycle"
    )
    assert normalized is not None
    assert status.startswith("wikiconv_lifecycle_event_time_")
    assert timezone == "America/New_York artifact corrected to UTC"


def _write_timestamp_evidence(tmp_path, snapshot, observations):
    output_root = tmp_path / "output"
    silver = output_root / "silver"
    bronze = tmp_path / "data" / "bronze"
    silver.mkdir(parents=True)
    bronze.mkdir(parents=True)
    (bronze / "mediawiki_revision_timestamps.json").write_text(
        json.dumps(snapshot), encoding="utf-8"
    )
    pq.write_table(
        pa.Table.from_pylist(observations),
        silver / "talk_page_revision_observations.parquet",
    )
    return output_root


def test_mediawiki_timestamp_loader_merges_snapshot_and_talk_revision_observations(
    tmp_path, monkeypatch
) -> None:
    output_root = _write_timestamp_evidence(
        tmp_path,
        {
            "101": {"status": "found", "timestamp": "2005-01-01T01:02:03Z"},
            "102": {"status": "not_found", "timestamp": "2005-01-01T01:02:04Z"},
        },
        [
            {
                "revision_id": 201,
                "timestamp": "2006-02-03T04:05:06Z",
                "availability_status": "content_available",
            },
            {
                "revision_id": 202,
                "timestamp": "2006-02-03T04:05:07Z",
                "availability_status": "revision_not_returned",
            },
        ],
    )

    def fail_on_whole_table_read(*args, **kwargs):
        raise AssertionError("timestamp evidence must be scanned in bounded batches")

    monkeypatch.setattr(pq, "read_table", fail_on_whole_table_read)

    assert _load_mediawiki_revision_timestamps(output_root) == {
        101: "2005-01-01T01:02:03+00:00",
        201: "2006-02-03T04:05:06+00:00",
    }


def test_mediawiki_timestamp_loader_accepts_exact_duplicate_evidence(tmp_path) -> None:
    output_root = _write_timestamp_evidence(
        tmp_path,
        {"301": {"status": "found", "timestamp": "2007-03-04T05:06:07Z"}},
        [
            {
                "revision_id": 301,
                "timestamp": "2007-03-04T05:06:07+00:00",
                "availability_status": "content_available",
            },
            {
                "revision_id": 301,
                "timestamp": "2007-03-04T05:06:07Z",
                "availability_status": "content_available",
            },
        ],
    )

    assert _load_mediawiki_revision_timestamps(output_root) == {301: "2007-03-04T05:06:07+00:00"}


def test_mediawiki_timestamp_loader_fails_closed_on_conflicting_evidence(tmp_path) -> None:
    output_root = _write_timestamp_evidence(
        tmp_path,
        {"401": {"status": "found", "timestamp": "2008-04-05T06:07:08Z"}},
        [
            {
                "revision_id": 401,
                "timestamp": "2008-04-05T06:07:09Z",
                "availability_status": "content_available",
            }
        ],
    )

    with pytest.raises(RuntimeError, match="conflict"):
        _load_mediawiki_revision_timestamps(output_root)
