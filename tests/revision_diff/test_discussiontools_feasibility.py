from __future__ import annotations

from wikidisputes_ssot.revision_diff.boundaries import BoundaryCandidate
from wikidisputes_ssot.revision_diff.discussiontools_feasibility import (
    FeasibilityResult,
    RenderedComment,
    RenderedMappingEvidence,
    evaluate_rendered_mapping,
    feasibility_report,
    select_feasibility_sample,
)


def _row(uid: str, **extra: object) -> dict[str, object]:
    return {"source_row_uid": uid, **extra}


def _candidate(**extra: object) -> BoundaryCandidate:
    values: dict[str, object] = {
        "candidate_uid": "candidate-1",
        "start": 0,
        "end": 20,
        "raw_wikitext": "hello [[User:Example]]",
        "body_start": 0,
        "body_end": 5,
        "body_wikitext": "hello",
        "signature_start": 6,
        "signature_end": 20,
        "raw_signature_wikitext": "[[User:Example]]",
        "signature_timestamp": "12:00, 1 January 2020 (UTC)",
        "signature_user_target": "Example",
        "indentation": "",
        "depth": 0,
        "boundary_evidence": ("terminal_signature",),
        "boundary_warnings": (),
    }
    values.update(extra)
    return BoundaryCandidate(**values)  # type: ignore[arg-type]


def test_sample_is_deterministic_disjoint_and_reports_shortfall():
    rows = [
        _row(
            f"a-{n}",
            method_a_status="promote",
            method_a_left_boundary=0,
            method_a_right_boundary=20,
            method_a_candidate_full_raw="raw",
            action_target_changed_ranges_json="[[0, 5]]",
        )
        for n in range(22)
    ]
    rows += [_row(f"b-{n}", method_b_status="b_safe") for n in range(22)]
    rows += [
        _row(f"creation-{n}", method_b_status="b_review", lifecycle="creation") for n in range(4)
    ]

    first = select_feasibility_sample(rows, seed="stable")
    second = select_feasibility_sample(rows, seed="stable")

    assert [item.source_row_uid for item in first.samples] == [
        item.source_row_uid for item in second.samples
    ]
    assert len({item.source_row_uid for item in first.samples}) == len(first.samples)
    assert first.selected["control_method_a_promote"] == 20
    assert first.selected["control_method_b_safe_usable"] == 20
    assert first.shortfalls["creation"] == 11
    creation = next(item for item in first.samples if item.source_row_uid.startswith("creation-"))
    assert creation.matching_labels == ("creation", "b_review")
    assert all(
        item.row.get("method_a_status") != "promote"
        for item in first.samples
        if not item.stratum.startswith("control_")
    )


def test_multi_action_label_accepts_revision_level_count():
    sample = select_feasibility_sample(
        [
            _row(
                "revision-actions",
                method_b_status="b_review",
                action_count_in_revision="2",
            )
        ],
        seed="stable",
    )
    selected = next(item for item in sample.samples if item.source_row_uid == "revision-actions")
    assert selected.stratum == "multi_action"
    assert selected.matching_labels == ("multi_action", "b_review")


def test_method_a_control_requires_a_nonempty_action_span():
    sample = select_feasibility_sample(
        [
            _row(
                "no-span",
                method_a_status="promote",
                method_a_left_boundary=0,
                method_a_right_boundary=20,
                method_a_candidate_full_raw="raw",
                action_target_changed_ranges_json="[]",
            )
        ],
        seed="stable",
    )
    assert sample.selected["control_method_a_promote"] == 0
    assert not sample.samples


def test_rendered_mapping_is_exact_and_fail_closed():
    candidate = _candidate()
    result = evaluate_rendered_mapping(
        [RenderedComment("hello", author="Example", timestamp="12:00, 1 January 2020 (UTC)")],
        [candidate],
        RenderedMappingEvidence(target_spans=((0, 5),), contamination_status="clean"),
    )
    assert result.safe
    assert result.provenance_tag == "rendered_structure_discussiontools"
    assert result.matched_candidate == candidate

    rejected = evaluate_rendered_mapping(
        [RenderedComment("hello", author="Other")],
        [candidate],
        RenderedMappingEvidence(target_spans=((0, 5),), contamination_status="detected"),
    )
    assert not rejected.safe
    assert rejected.matched_candidate == candidate
    assert rejected.failure_reasons == ("author_mismatch", "contamination_detected")


def test_gate_requires_controls_and_meaningful_clean_residual_yield():
    results = [
        FeasibilityResult(
            f"control-{index}",
            "control_method_a_promote",
            True,
            True,
            exact_boundary_agreement=True,
            b_status="b_safe",
            lifecycle="creation",
            contamination_status="clean",
        )
        for index in range(40)
    ]
    results += [
        FeasibilityResult(
            f"residual-{index}",
            "b_review",
            False,
            True,
            proposed_safe=index < 10,
            b_status="b_review",
            lifecycle="modification",
            contamination_status="clean",
        )
        for index in range(160)
    ]
    report = feasibility_report(results)
    assert report.gate.passed
    assert report.residual["unique_safe_count"] == 10
    assert report.controls["exact_boundary_agreement_rate"] == 1.0


def test_unknown_contamination_fails_closed_and_blocks_gate():
    candidate = _candidate()
    mapping = evaluate_rendered_mapping(
        [RenderedComment("hello")],
        [candidate],
        RenderedMappingEvidence(target_spans=((0, 5),)),
    )
    assert not mapping.safe
    assert mapping.matched_candidate == candidate
    assert mapping.failure_reasons == ("contamination_unknown",)

    report = feasibility_report(
        [
            FeasibilityResult(
                "row-1",
                "b_review",
                False,
                True,
                proposed_safe=True,
                contamination_status="unknown",
                b_status="b_review",
            )
        ]
    )
    assert report.residual["contamination_status_counts"] == {
        "clean": 0,
        "detected": 0,
        "unknown": 1,
    }
    assert "proposed_safe_contamination_unknown" in report.gate.reasons
