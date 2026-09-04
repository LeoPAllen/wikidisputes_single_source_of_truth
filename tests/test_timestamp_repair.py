from wikidisputes_ssot.full import (
    _repair_wikiconv_creation_timestamp,
)


def test_winter_eastern_artifact_repair() -> None:
    repaired, status = (
        _repair_wikiconv_creation_timestamp(
            "2007-01-12T07:22:47+00:00"
        )
    )

    assert repaired == "2007-01-12T02:22:47+00:00"
    assert status == (
        "wikiconv_creation_time_corrected_eastern_artifact"
    )


def test_summer_eastern_artifact_repair() -> None:
    repaired, status = (
        _repair_wikiconv_creation_timestamp(
            "2012-07-20T04:59:12+00:00"
        )
    )

    assert repaired == "2012-07-20T00:59:12+00:00"
    assert status == (
        "wikiconv_creation_time_corrected_eastern_artifact"
    )


def test_missing_timestamp_remains_missing() -> None:
    repaired, status = (
        _repair_wikiconv_creation_timestamp(None)
    )

    assert repaired is None
    assert status == "wikiconv_creation_time_unavailable"
