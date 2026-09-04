from __future__ import annotations

import pytest

from wikidisputes_ssot.revision_diff.models import MethodBEvidence


def _evidence(**changes: object) -> MethodBEvidence:
    values: dict[str, object] = {
        "source_row_uid": "source-1",
        "logical_utterance_uid": "logical-1",
        "action_uid": "action-1",
        "action_type": "creation",
        "target_revision_id": "2",
        "predecessor_revision_id": "1",
        "page_id": "3",
        "target_availability": "available",
        "predecessor_availability": "available",
    }
    values.update(changes)
    return MethodBEvidence(**values)  # type: ignore[arg-type]


def test_evidence_accepts_explicit_localization_and_contamination_fields() -> None:
    evidence = _evidence(
        whole_page_candidate_count=100,
        localized_candidate_count=2,
        localization_evidence_json='["changed_span_overlap"]',
        action_target_changed_ranges_json="[[12, 24]]",
        hunk_attribution_evidence_json='["informative_text_unique"]',
        assignment_reason_codes_json='["assigned"]',
        neighboring_comment_contamination="clean",
    )

    assert evidence.whole_page_candidate_count == 100
    assert evidence.localized_candidate_count == 2
    assert evidence.neighboring_comment_contamination == "clean"


@pytest.mark.parametrize("value", ["", "false", "not_evaluated", True])
def test_evidence_rejects_invalid_contamination_state(value: object) -> None:
    with pytest.raises(ValueError, match="neighboring_comment_contamination"):
        _evidence(neighboring_comment_contamination=value)


@pytest.mark.parametrize(
    "field",
    [
        "localization_evidence_json",
        "action_target_changed_ranges_json",
        "hunk_attribution_evidence_json",
        "assignment_reason_codes_json",
    ],
)
def test_evidence_requires_new_evidence_fields_to_encode_lists(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _evidence(**{field: "{}"})
