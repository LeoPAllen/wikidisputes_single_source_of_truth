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
