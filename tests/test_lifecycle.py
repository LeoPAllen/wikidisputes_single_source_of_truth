from __future__ import annotations

import json

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


def test_creation_identity_not_timestamp_controls_utterance_order() -> None:
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
    assert [row[1].split(".", 1)[0] for row in ordered] == [
        "449109941",
        "449117947",
        "449120907",
        "449122039",
        "449143272",
    ]


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
