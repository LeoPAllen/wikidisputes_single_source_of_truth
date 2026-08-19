from __future__ import annotations

from wikidisputes_ssot.constants import CROSS_LABEL_DISCUSSION_IDS, CURRENT
from wikidisputes_ssot.cross_label import resolve_cross_label_policy
from wikidisputes_ssot.events_dv import normalize_tag_family
from wikidisputes_ssot.full import _append_only_registry
from wikidisputes_ssot.reverts import detect_identity_reverts
from wikidisputes_ssot.source import _row_uid


def test_source_identity_ignores_content_and_export_order() -> None:
    one = _row_uid(CURRENT, "data/escalated.json", "escalated", 3, 4)
    rerun = _row_uid(CURRENT, "data/escalated.json", "escalated", 3, 4)
    moved = _row_uid(CURRENT, "data/escalated.json", "escalated", 3, 5)
    assert one == rerun
    assert one != moved


def test_identity_revert_algorithm() -> None:
    rows = [
        {"revision_id": 1, "sha1": "a"},
        {"revision_id": 2, "sha1": "b"},
        {"revision_id": 3, "sha1": "c"},
        {"revision_id": 4, "sha1": "a"},
        {"revision_id": 5, "sha1": "a"},
    ]
    reverts = detect_identity_reverts(rows)
    assert len(reverts) == 1
    assert reverts[0].reverting_revision_id == "4"
    assert reverts[0].reverted_revision_ids == ("2", "3")


def test_all_mandatory_cross_label_fixture_ids_are_pinned() -> None:
    assert set(CROSS_LABEL_DISCUSSION_IDS) == {
        "514025327.9787.9787",
        "545620272.7408.7408",
        "509266297.83325.83325",
        "512260994.112001.112001",
        "502277435.16708.16708",
    }


def test_cross_label_policy_never_emits_a_contradictory_binary_value() -> None:
    positive = resolve_cross_label_policy(same_episode=True, formal_escalation_verified=True)
    distinct = resolve_cross_label_policy(same_episode=False, formal_escalation_verified=True)
    unresolved = resolve_cross_label_policy(same_episode=None, formal_escalation_verified=True)
    assert positive["analytic_outcome"] is True
    assert distinct["analytic_outcome"] is None
    assert unresolved["analytic_outcome"] is None
    assert "quarantined" in unresolved["analytic_status"]


def test_identity_registry_is_append_only_and_never_rewrites_issued_entry() -> None:
    existing = [{"registry_entry_uid": "r1", "issued_uid": "fallback:old"}]
    derived = [
        {"registry_entry_uid": "r1", "issued_uid": "wikiconv:new"},
        {"registry_entry_uid": "r2", "issued_uid": "wikiconv:2"},
    ]
    merged = _append_only_registry(existing, derived)
    assert merged == [
        {"registry_entry_uid": "r1", "issued_uid": "fallback:old"},
        {"registry_entry_uid": "r2", "issued_uid": "wikiconv:2"},
    ]


def test_tag_family_normalization_is_versioned_and_keeps_raw_input_separate() -> None:
    assert normalize_tag_family("{{NPOV|date=August 2008}}") == "neutral_point_of_view"
    assert normalize_tag_family("{{Totally-disputed-section}}") == "totally_disputed"
    assert normalize_tag_family("{{Disputed-inline}}") == "disputed"
