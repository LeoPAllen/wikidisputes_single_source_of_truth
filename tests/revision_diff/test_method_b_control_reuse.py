from __future__ import annotations

import pytest

from wikidisputes_ssot.revision_diff.workflow import (
    merge_pilot_control_evidence,
    partition_baseline_controls,
)


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
