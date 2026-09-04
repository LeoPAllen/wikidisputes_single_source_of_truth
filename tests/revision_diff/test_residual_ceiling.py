from __future__ import annotations

import pytest

from wikidisputes_ssot.revision_diff.residual_ceiling import (
    B_UNAVAILABLE,
    derive_residual_rows,
    deterministic_stratified_sample,
    summarize_completed_labels,
)


def _source(uid: str, lifecycle: str = "creation") -> dict[str, object]:
    return {"source_row_uid": uid, "action_type": lifecycle, "source_text": uid}


def _recovery(
    uid: str,
    status: str,
    lifecycle: str = "creation",
    **extra: object,
) -> dict[str, object]:
    return {
        "source_row_uid": uid,
        "status": status,
        "action_type": lifecycle,
        "reason_codes_json": '["no_candidate"]',
        **extra,
    }


def _selection(uid: str, selected: str = "method_a_fallback") -> dict[str, str]:
    return {"source_row_uid": uid, "selected_method": selected}


def test_derivation_uses_frozen_selection_and_rejects_missing_evidence() -> None:
    sources = [_source("a"), _source("b")]
    recovery = [_recovery("a", "b_no_candidate")]
    selection = [_selection("a"), _selection("b", "method_a")]
    residual, total = derive_residual_rows(sources, recovery, selection)
    assert total == 2
    assert [row["source_row_uid"] for row in residual] == ["a"]

    with pytest.raises(ValueError, match="no recovery evidence"):
        derive_residual_rows(sources, recovery, [_selection("a"), _selection("b")])


def test_two_stage_sample_censuses_token_and_records_cell_weights() -> None:
    rows = (
        [
            {
                "source_row_uid": f"token-{number}",
                "method_b_status": "b_ambiguous",
                "action_type": "modification",
                "token_persistence": True,
            }
            for number in range(2)
        ]
        + [
            {
                "source_row_uid": f"normal-{number}",
                "method_b_status": "b_ambiguous",
                "action_type": "modification",
            }
            for number in range(10)
        ]
        + [
            {
                "source_row_uid": f"dt-{number}",
                "method_b_status": "b_review",
                "action_type": "creation",
                "discussiontools_evidence": True,
            }
            for number in range(3)
        ]
        + [
            {
                "source_row_uid": f"other-{number}",
                "method_b_status": "b_review",
                "action_type": "creation",
            }
            for number in range(8)
        ]
        + [
            {
                "source_row_uid": "unavailable",
                "method_b_status": B_UNAVAILABLE,
                "action_type": "creation",
            }
        ]
    )
    plan = deterministic_stratified_sample(rows, requested_size=12, min_per_stratum=6)
    assert len(plan.sampled) == 12
    assert plan.unavailable_count == 1
    assert {row["source_row_uid"] for row in plan.sampled} >= {"token-0", "token-1"}
    token_rows = [row for row in plan.sampled if row["diagnostic_domain"] == "token_persistence"]
    assert {row["inclusion_probability"] for row in token_rows} == {1.0}
    assert {row["survey_weight"] for row in token_rows} == {1.0}
    dt_rows = [
        row for row in plan.sampled if row["diagnostic_domain"] == "discussiontools_evidence"
    ]
    assert len(dt_rows) == 2
    assert dt_rows[0]["inclusion_probability"] == 2 / 3
    assert sum(cell["sample_n"] for cell in plan.design_cells) == 12
    assert sum(stratum["sample_n"] for stratum in plan.primary_strata) == 12


def test_summary_uses_weights_excludes_unavailable_and_has_ceiling_accounting() -> None:
    rows = [
        {
            "source_row_uid": f"eligible-{number}",
            "method_b_status": "b_review",
            "action_type": "creation",
            "failure_reasons": ["no_candidate"],
        }
        for number in range(4)
    ] + [
        {
            "source_row_uid": "unavailable",
            "method_b_status": B_UNAVAILABLE,
            "action_type": "creation",
        }
    ]
    plan = deterministic_stratified_sample(rows, requested_size=4)
    labels = {
        "eligible-0": {"recoverability": "existing_evidence_exact", "rule_family": ""},
        "eligible-1": {"recoverability": "deterministic_rule_possible", "rule_family": "diff"},
        "eligible-2": {"recoverability": "human_only", "rule_family": ""},
        "eligible-3": {"recoverability": "no_identifiable_comment", "rule_family": ""},
    }
    report = summarize_completed_labels(plan, labels, total_population=10)
    assert report["design"]["validated_a_plus_b_exact_count"] == 5
    assert report["design"]["b_unavailable_exact_count"] == 1
    assert report["recoverability_classes"]["existing_evidence_exact"]["estimated_count"] == 1
    assert (
        report["breakdowns"]["b_status"]["b_review"]["recoverability_classes"][
            "existing_evidence_exact"
        ]["estimated_count"]
        == 1
    )
    assert (
        report["ceilings"]["current_evidence"]["additional_recoverable_residual"][
            "estimated_percent"
        ]
        == 20
    )
    assert (
        report["ceilings"]["current_evidence"]["implied_total_ssot_coverage"]["estimated_count"]
        == 6
    )
    assert (
        report["ceilings"]["deterministic_engineering"]["implied_total_ssot_coverage"][
            "estimated_count"
        ]
        == 7
    )
    assert (
        report["ceilings"]["human_assisted"]["implied_total_ssot_coverage"]["estimated_count"] == 8
    )


def test_summary_requires_all_labels() -> None:
    plan = deterministic_stratified_sample(
        [{"source_row_uid": "one", "method_b_status": "b_review", "action_type": "creation"}],
        requested_size=1,
    )
    with pytest.raises(ValueError, match="completed labels"):
        summarize_completed_labels(plan, {}, total_population=2)
