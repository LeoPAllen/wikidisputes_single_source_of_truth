from __future__ import annotations

from wikidisputes_ssot.method_a_promotion import action_for_recovery


def test_recovery_maps_by_frozen_source_and_action_after_logical_uid_change() -> None:
    recovery = {
        "source_row_uid": "source-1",
        "utterance_id": "123.4.4",
        "logical_utterance_uid": "old-logical",
    }
    current_action = {
        "action_uid": "current-action",
        "logical_utterance_uid": "current-logical",
        "action_id_exact": "123.4.4",
    }
    key = ("source-1", "123.4.4")
    assert action_for_recovery(recovery, {key: [current_action]}) is current_action
    assert action_for_recovery(recovery, {key: [current_action, current_action]}) is None
