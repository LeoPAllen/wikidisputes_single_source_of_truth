from __future__ import annotations

import json

from wikidisputes_ssot.full import _wikiconv_lifecycle


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
