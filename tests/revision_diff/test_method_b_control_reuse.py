from __future__ import annotations

import pytest

from wikidisputes_ssot.revision_diff.workflow import (
    _immutable_annotation_comparison,
    _structure_matches_canonical,
    merge_pilot_control_evidence,
    partition_baseline_controls,
)

_STRUCTURAL_FIELDS = (
    "logical_utterance_uid",
    "action_uid",
    "utterance_order",
    "reply_target_logical_uid",
    "dispute_uid",
    "episode_uid",
    "conversation_uid",
)


def _structure_row(source_uid: str = "source-1") -> dict[str, object]:
    return {
        "source_row_uid": source_uid,
        "logical_utterance_uid": "logical-1",
        "action_uid": "action-1",
        "utterance_order": 7,
        "reply_target_logical_uid": "logical-parent",
        "dispute_uid": "dispute-1",
        "episode_uid": "episode-1",
        "conversation_uid": "conversation-1",
        "utterance_text": "original wording",
        "normalized_text": "original wording",
    }


def test_structure_match_ignores_text_changes() -> None:
    source = _structure_row()
    canonical = _structure_row()
    canonical["utterance_text"] = "selected Method B wording"
    canonical["normalized_text"] = "selected Method B wording"

    assert _structure_matches_canonical([source], [canonical])


@pytest.mark.parametrize("field", _STRUCTURAL_FIELDS)
def test_structure_match_rejects_each_structural_change(field: str) -> None:
    source = _structure_row()
    canonical = _structure_row()
    canonical[field] = "changed-structure"

    assert not _structure_matches_canonical([source], [canonical])


def test_structure_match_requires_same_per_source_rows() -> None:
    source = [_structure_row("source-1"), _structure_row("source-2")]
    canonical = [_structure_row("source-2"), _structure_row("source-1")]

    assert _structure_matches_canonical(source, canonical)
    assert not _structure_matches_canonical(source, [_structure_row("source-1")])


def test_method_b_invariant_uses_pre_overlay_baseline(tmp_path) -> None:
    baseline = tmp_path / "baseline.csv"
    staged = tmp_path / "staged.csv"
    baseline.write_text(
        "ssot_source_row_uid,utterance_order,reply_to_utterance_id,utterance_text,"
        "ssot_annotation_text_source\nrow-1,1,,before,method_a\n",
        encoding="utf-8",
    )
    staged.write_text(
        "ssot_source_row_uid,utterance_order,reply_to_utterance_id,utterance_text,"
        "ssot_annotation_text_source\nrow-1,1,,after,method_b\n",
        encoding="utf-8",
    )

    assert _immutable_annotation_comparison(baseline, staged)["passed"] is True

    staged.write_text(
        "ssot_source_row_uid,utterance_order,reply_to_utterance_id,utterance_text,"
        "ssot_annotation_text_source\nrow-1,2,,after,method_b\n",
        encoding="utf-8",
    )
    comparison = _immutable_annotation_comparison(baseline, staged)
    assert comparison["passed"] is False
    assert comparison["mismatch_fields"] == {"utterance_order": 1}


def test_method_b_invariant_allows_only_correct_derived_text_flag(tmp_path) -> None:
    baseline = tmp_path / "baseline.csv"
    staged = tmp_path / "staged.csv"
    headers = (
        "ssot_source_row_uid,utterance_text,ssot_source_text_exact,ssot_text_differs_from_source\n"
    )
    baseline.write_text(headers + "row-1,source,source,false\n", encoding="utf-8")
    staged.write_text(headers + "row-1,recovered,source,true\n", encoding="utf-8")

    assert _immutable_annotation_comparison(baseline, staged)["passed"] is True

    staged.write_text(headers + "row-1,recovered,source,false\n", encoding="utf-8")
    comparison = _immutable_annotation_comparison(baseline, staged)
    assert comparison["passed"] is False
    assert comparison["text_difference_flag_mismatch_rows"] == 1


def _source(
    uid: str = "source-1", *, action: str = "action-1", revision: int = 42
) -> dict[str, object]:
    return {
        "source_row_uid": uid,
        "action_uid": action,
        "logical_utterance_uid": f"logical-{uid}",
        "revision_id": revision,
    }


def _evidence(uid: str = "source-1", *, status: str = "b_safe") -> dict[str, object]:
    return {
        "source_row_uid": uid,
        "action_uid": "action-1",
        "logical_utterance_uid": f"logical-{uid}",
        "target_revision_id": 42,
        "status": status,
        "candidate_body": "unchanged baseline",
    }


def test_partition_retains_only_selectable_rows_and_preserves_fields() -> None:
    safe = _evidence()
    usable = _evidence("source-2", status="b_usable")
    review = _evidence("source-3", status="b_review")
    controls, uids = partition_baseline_controls(
        [safe, usable, review],
        [_source(), _source("source-2"), _source("source-3")],
    )

    assert controls == [safe, usable]
    assert controls[0] is safe
    assert uids == frozenset({"source-1", "source-2"})


@pytest.mark.parametrize(
    "field, value",
    [
        ("action_uid", "different-action"),
        ("logical_utterance_uid", "different-logical"),
        ("target_revision_id", 99),
    ],
)
def test_selectable_identity_mismatch_is_rejected(field: str, value: object) -> None:
    baseline = _evidence()
    baseline[field] = value
    with pytest.raises(ValueError, match="identity mismatch"):
        partition_baseline_controls([baseline], [_source()])


def test_missing_source_and_duplicate_baseline_controls_are_rejected() -> None:
    with pytest.raises(ValueError, match="absent from current source"):
        partition_baseline_controls([_evidence("missing")], [_source()])
    with pytest.raises(ValueError, match="duplicate baseline"):
        partition_baseline_controls([_evidence(), _evidence()], [_source()])


def test_nonselectable_identity_mismatch_is_not_a_control_error() -> None:
    review = _evidence(status="b_review")
    review["action_uid"] = "stale-action"
    controls, uids = partition_baseline_controls([review], [_source()])
    assert controls == []
    assert uids == frozenset()


def test_primary_evidence_supersedes_stale_pilot_control() -> None:
    primary = _evidence(status="b_safe")
    pilot = _evidence(status="b_no_candidate")
    merged = merge_pilot_control_evidence([primary], [pilot])
    assert merged == [primary]


def test_pilot_control_supplements_missing_primary_evidence() -> None:
    primary = _evidence()
    pilot = _evidence("source-2", status="b_usable")
    merged = merge_pilot_control_evidence([primary], [pilot])
    assert merged == [primary, pilot]
