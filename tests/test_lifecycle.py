from __future__ import annotations

import json

import pytest

from wikidisputes_ssot.full import (
    _creation_order_key,
    _logical_creator_speaker,
    _source_logical_anchor,
    _wikiconv_lifecycle,
)


def test_nested_wikiconv_lifecycle_is_flattened_without_new_turns() -> None:
    original = {
        "id": "10.2.2",
        "speaker": {"id": "Alice", "speaker_id": "7"},
        "reply_to": "9.1.1",
        "timestamp": 100.0,
        "text": "old",
        "meta_dict": {"rev_id": "10", "parent_id": None},
    }
    modification = {
        "id": "11.2.2",
        "speaker": {"id": "Alice", "speaker_id": "7"},
        "reply_to": "9.1.1",
        "timestamp": 110.0,
        "text": "new",
        "meta_dict": {"rev_id": "11", "parent_id": "10.2.2"},
    }
    deletion = {
        "id": "12.2.2",
        "speaker": {"id": "Bob", "speaker_id": "8"},
        "reply_to": None,
        "timestamp": 120.0,
        "text": "new",
        "meta_dict": {"rev_id": "12", "parent_id": "11.2.2"},
    }
    row = {
        "wikiconv_id_exact": "12.2.2",
        "ancestor_id_exact": "10.2.2",
        "wikiconv_speaker_exact": "Bob",
        "conversation_id_exact": "1.1.1",
        "wikiconv_reply_to_exact": None,
        "wikiconv_timestamp_unix": 120.0,
        "wikiconv_text_exact": " ",
        "meta_json_canonical": json.dumps(
            {
                "original": original,
                "modification": [modification],
                "deletion": [deletion],
                "restoration": [],
            }
        ),
    }
    actions = _wikiconv_lifecycle(row)
    assert [action["action_type"] for action in actions] == [
        "creation",
        "modification",
        "deletion",
    ]
    assert {action["id"] for action in actions} == {"10.2.2", "11.2.2", "12.2.2"}


def test_known_creation_timestamp_controls_utterance_order() -> None:
    rows = [
        ("current-1", "449109941.23216.23182", "2011-09-08T12:00:00+00:00"),
        ("current-2", "449117947.24220.24220", "2011-09-08T11:49:05+00:00"),
        ("current-3", "449120907.24583.24582", "2011-09-08T11:22:20+00:00"),
        ("current-4", "449122039.25159.25159", "2011-09-08T11:33:30+00:00"),
        ("current-5", "449143272.25393.25393", "2011-09-08T15:33:14+00:00"),
    ]
    ordered = sorted(
        rows,
        key=lambda row: _creation_order_key(
            {"creation_id": row[1], "source_order": 0, "created_at": row[2]}, row[0]
        ),
    )
    assert [row[2] for row in ordered] == sorted(row[2] for row in rows)


@pytest.fixture
def talk_guru_case_1686() -> list[tuple[str, str, str, int]]:
    return [
        ("guru-root", "13490800.105758.105758", "2005-05-09T17:35:16+00:00", 1),
        ("guru-later", "13480142.105967.105967", "2005-05-09T18:28:35+00:00", 2),
    ]


def test_talk_guru_case_1686_creation_time_prevents_revision_id_inversion(
    talk_guru_case_1686: list[tuple[str, str, str, int]],
) -> None:
    rows = talk_guru_case_1686
    ordered = sorted(
        rows,
        key=lambda row: _creation_order_key(
            {"creation_id": row[1], "created_at": row[2], "source_order": row[3]}, row[0]
        ),
    )
    assert [row[0] for row in ordered] == ["guru-root", "guru-later"]


@pytest.fixture
def talk_motorway_case_6214() -> list[tuple[str, str, str, int]]:
    return [
        ("motorway-2003-a", "12091907.949.0", "2003-01-04T00:25:07+00:00", 1),
        ("motorway-2003-b", "12091907.964.0", "2003-01-04T00:25:07+00:00", 2),
        ("motorway-2004-a", "4315595.964.949", "2004-06-25T22:24:36+00:00", 3),
        ("motorway-2004-b", "4316006.1328.1328", "2004-06-27T19:04:19+00:00", 4),
    ]


def test_talk_motorway_case_6214_january_2003_precedes_2004(
    talk_motorway_case_6214: list[tuple[str, str, str, int]],
) -> None:
    rows = talk_motorway_case_6214
    ordered = sorted(
        rows,
        key=lambda row: _creation_order_key(
            {"creation_id": row[1], "created_at": row[2], "source_order": row[3]}, row[0]
        ),
    )
    assert [row[0] for row in ordered] == [
        "motorway-2003-a",
        "motorway-2003-b",
        "motorway-2004-a",
        "motorway-2004-b",
    ]


def test_equal_time_uses_numeric_creation_position_then_stable_fallbacks() -> None:
    timestamp = "2005-05-09T17:35:16+00:00"
    earlier = {"creation_id": "100.4.9", "created_at": timestamp, "source_order": 8}
    later = {"creation_id": "100.5.1", "created_at": timestamp, "source_order": 1}
    assert _creation_order_key(earlier, "z") < _creation_order_key(later, "a")


def test_unknown_time_order_is_deterministic_and_after_known_times() -> None:
    known = {"creation_id": "999.9.9", "created_at": "2020-01-01T00:00:00Z", "source_order": 9}
    unknown_a = {"creation_id": "2.2.2", "created_at": None, "source_order": 2}
    unknown_b = {"creation_id": "1.1.1", "created_at": None, "source_order": 1}
    assert _creation_order_key(known, "known") < _creation_order_key(unknown_b, "b")
    assert _creation_order_key(unknown_b, "b") < _creation_order_key(unknown_a, "a")


def test_lifecycle_action_uses_authoritative_original_identity() -> None:
    assert (
        _source_logical_anchor(
            {
                "wikidisputes_type_exact": "modification",
                "wikidisputes_id_exact": "456481516.21427.21427",
                "wikidisputes_original_id_exact": "456479438.20594.20594",
            }
        )
        == "456479438.20594.20594"
    )
    assert (
        _source_logical_anchor(
            {
                "wikidisputes_type_exact": "modification",
                "wikidisputes_id_exact": "714748613.96746.96746",
                "wikidisputes_original_id_exact": None,
            }
        )
        == "714748613.96746.96746"
    )


def test_logical_speaker_comes_from_creation_not_modifier() -> None:
    row = {
        "meta_json_canonical": json.dumps(
            {
                "original": {"id": "10.2.2", "speaker": "creator"},
                "modification": [{"id": "11.2.2", "speaker": "modifier"}],
            }
        ),
        "wikiconv_speaker_exact": "modifier",
    }
    assert _logical_creator_speaker([row]) == "creator"
