from __future__ import annotations

from copy import deepcopy

from wikidisputes_ssot.full import _reply_constrained_display_order


def _creation(uid: str, created_at: str | None, source_order: int) -> dict[str, object]:
    return {
        "conversation_id": "conversation-1",
        "creation_id": uid,
        "created_at": created_at,
        "source_order": source_order,
        "chronology_rank": None,
    }


def test_reply_constraints_bound_unknowns_without_assigning_chronology() -> None:
    creations = {
        "known-parent": _creation("1.1.1", "2020-01-01T10:00:00+00:00", 1),
        "unknown-parent": _creation("2.1.1", None, 2),
        "known-middle": _creation("3.1.1", "2020-01-01T11:00:00+00:00", 3),
        "known-child": _creation("4.1.1", "2020-01-01T12:00:00+00:00", 4),
        "unknown-child": _creation("5.1.1", None, 5),
    }
    original = deepcopy(creations)
    replies = {
        "known-parent": None,
        "unknown-parent": None,
        "known-middle": None,
        "known-child": "unknown-parent",
        "unknown-child": "known-parent",
    }

    ordered, evidence = _reply_constrained_display_order(
        logical_uids=list(reversed(creations)),
        creation_by_logical=creations,
        reply_target_by_source=replies,
    )

    assert ordered.index("known-middle") < ordered.index("unknown-parent")
    assert ordered.index("unknown-parent") < ordered.index("known-child")
    assert ordered.index("known-parent") < ordered.index("unknown-child")
    assert ordered.index("unknown-child") < ordered.index("known-middle")
    assert evidence["unknown-parent"]["reply_upper_bound_utc"] == ("2020-01-01T12:00:00+00:00")
    assert evidence["unknown-child"]["reply_lower_bound_utc"] == ("2020-01-01T10:00:00+00:00")
    assert creations == original
    assert all(row["chronology_rank"] is None for row in creations.values())


def test_ambiguous_unknowns_use_fewer_direct_replies_then_stable_keys() -> None:
    creations = {
        "busy": _creation("3.1.1", None, 3),
        "busy-child": _creation("4.1.1", None, 4),
        "quiet-a": _creation("1.1.1", None, 1),
        "quiet-b": _creation("2.1.1", None, 2),
    }
    replies = {
        "busy": None,
        "busy-child": "busy",
        "quiet-a": None,
        "quiet-b": None,
    }

    forward, _ = _reply_constrained_display_order(
        logical_uids=list(creations),
        creation_by_logical=creations,
        reply_target_by_source=replies,
    )
    reverse, _ = _reply_constrained_display_order(
        logical_uids=list(reversed(creations)),
        creation_by_logical=creations,
        reply_target_by_source=replies,
    )

    assert forward == reverse
    assert forward.index("quiet-a") < forward.index("quiet-b") < forward.index("busy")
    assert forward.index("busy") < forward.index("busy-child")


def test_modification_action_time_bounds_unresolved_creation_without_rewriting_it() -> None:
    creations = {
        "known-early": _creation("1.1.1", "2020-01-01T10:00:00+00:00", 1),
        "unknown-modified": _creation("2.1.1", None, 2),
        "known-late": _creation("3.1.1", "2020-01-01T12:00:00+00:00", 3),
    }

    ordered, evidence = _reply_constrained_display_order(
        logical_uids=list(creations),
        creation_by_logical=creations,
        reply_target_by_source={},
        action_creation_upper_bound_by_uid={"unknown-modified": "2020-01-01T11:00:00+00:00"},
    )

    assert ordered == ["known-early", "unknown-modified", "known-late"]
    assert creations["unknown-modified"]["created_at"] is None
    assert creations["unknown-modified"]["chronology_rank"] is None
    assert evidence["unknown-modified"]["action_creation_upper_bound_utc"] == (
        "2020-01-01T11:00:00+00:00"
    )
    assert evidence["unknown-modified"]["action_bound_status"] == "applied"


def test_action_and_reply_bounds_limit_deterministic_fallback_to_feasible_interval() -> None:
    creations = {
        "known-parent": _creation("1.1.1", "2020-01-01T10:00:00+00:00", 1),
        "quiet": _creation("2.1.1", None, 2),
        "busy": _creation("3.1.1", None, 3),
        "busy-child": _creation("4.1.1", None, 4),
        "known-middle": _creation("5.1.1", "2020-01-01T10:30:00+00:00", 5),
        "known-late": _creation("6.1.1", "2020-01-01T12:00:00+00:00", 6),
    }
    replies = {
        "quiet": "known-parent",
        "busy": "known-parent",
        "busy-child": "busy",
    }
    bounds = {
        "quiet": "2020-01-01T11:00:00+00:00",
        "busy": "2020-01-01T11:00:00+00:00",
        "busy-child": "2020-01-01T11:00:00+00:00",
    }

    ordered, _ = _reply_constrained_display_order(
        logical_uids=list(reversed(creations)),
        creation_by_logical=creations,
        reply_target_by_source=replies,
        action_creation_upper_bound_by_uid=bounds,
    )

    assert ordered.index("known-parent") < ordered.index("known-middle")
    assert ordered.index("known-middle") < ordered.index("quiet")
    assert ordered.index("quiet") < ordered.index("busy")
    assert ordered.index("busy") < ordered.index("busy-child")
    assert ordered.index("busy-child") < ordered.index("known-late")


def test_action_bound_never_changes_known_creation_order() -> None:
    creations = {
        "known-early": _creation("1.1.1", "2020-01-01T10:00:00+00:00", 1),
        "known-late": _creation("2.1.1", "2020-01-01T12:00:00+00:00", 2),
    }

    ordered, evidence = _reply_constrained_display_order(
        logical_uids=list(reversed(creations)),
        creation_by_logical=creations,
        reply_target_by_source={},
        action_creation_upper_bound_by_uid={"known-late": "2020-01-01T09:00:00+00:00"},
    )

    assert ordered == ["known-early", "known-late"]
    assert evidence["known-late"]["action_creation_upper_bound_utc"] is None
