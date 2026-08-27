from wikidisputes_ssot.revision_diff.reporting import (
    blinded_audit_packet,
    localization_fix_comparison_report,
    pilot_validation_report,
    pilot_validation_rows,
    profile_rows,
    recovery_report,
    select_stratified_pilot,
)

ROWS = [
    {
        "id": "one",
        "method_a_status": "fallback",
        "method_a_reasons": "low_margin",
        "target_text": "",
        "candidate_raw_body": "[[A|alpha]]",
        "action_class": "addition",
        "revision_available": True,
        "predecessor_available": False,
    },
    {
        "id": "two",
        "method_a_status": "promote",
        "target_text": "No 2019",
        "candidate_raw_body": "No 2019",
        "action_class": "modification",
        "lifecycle": "available",
        "recovered": True,
        "markup_categories": ["link"],
    },
    {
        "id": "three",
        "method_a_status": "review",
        "target_text": "Restored",
        "candidate_raw_body": "Restored",
        "action_class": "restoration",
        "multi_action_revision": True,
        "lifecycle": "missing",
    },
]


def test_profile_captures_review_population_and_availability() -> None:
    profile = {row["entity_uid"]: row for row in profile_rows(ROWS)}
    assert profile["one"]["fallback_or_review"]
    assert profile["one"]["empty_target"]
    assert profile["three"]["multi_action_revision"]


def test_pilot_is_deterministic_and_has_required_overlapping_strata() -> None:
    first = select_stratified_pilot(ROWS, seed=7, per_stratum=1)
    assert first == select_stratified_pilot(reversed(ROWS), seed=7, per_stratum=1)
    assert {entry["stratum"] for entry in first["strata_manifest"]} >= {
        "fallback_or_review",
        "empty_target",
        "addition",
        "modification",
        "restoration",
        "multi_action",
        "difficult",
        "a_safe_control",
    }


def test_validation_compares_bodies_and_boundaries_without_truth_claim() -> None:
    result = pilot_validation_rows(
        [
            {
                "id": "x",
                "method_a_candidate_raw_body": "[[A|alpha]]",
                "method_b_candidate_raw_body": "alpha",
                "method_a_left_boundary": 1,
                "method_b_left_boundary": 1,
            }
        ]
    )[0]
    assert result["comparison_reference"] == "method_a_not_ground_truth"
    assert result["candidate"]["visible_equal"]
    assert result["left_boundary_equal"]


def test_validation_does_not_compare_missing_text_or_boundaries() -> None:
    result = pilot_validation_rows([{"id": "missing"}])[0]
    comparison = result["candidate"]
    assert comparison["left_present"] is False
    assert comparison["right_present"] is False
    assert comparison["comparable"] is False
    assert comparison["raw_equal"] is None
    assert comparison["visible_equal"] is None
    assert comparison["missing_critical_tokens"] is None
    assert comparison["added_critical_tokens"] is None
    assert result["critical_token_difference"] is None
    assert result["left_boundary_comparable"] is False
    assert result["right_boundary_comparable"] is False
    assert result["left_boundary_equal"] is None
    assert result["right_boundary_equal"] is None


def test_validation_honors_explicit_unavailable_flags_for_empty_serialization() -> None:
    unavailable = pilot_validation_rows(
        [
            {
                "id": "missing-as-empty",
                "method_a_candidate_raw_body": "",
                "method_b_candidate_raw_body": "",
                "method_a_candidate_available": False,
                "method_b_candidate_available": False,
            }
        ]
    )[0]
    assert unavailable["candidate"]["comparable"] is False
    assert unavailable["candidate"]["raw_equal"] is None

    available = pilot_validation_rows(
        [
            {
                "id": "present-empty",
                "method_a_candidate_raw_body": "",
                "method_b_candidate_raw_body": "",
                "method_a_candidate_available": True,
                "method_b_candidate_available": True,
            }
        ]
    )[0]
    assert available["candidate"]["comparable"] is True
    assert available["candidate"]["raw_equal"] is True


def test_validation_reports_comparable_denominators_and_contamination_states() -> None:
    report = pilot_validation_report(
        [
            {
                "id": "equal",
                "action_type": "creation",
                "method_a_candidate_raw_body": "not 1",
                "method_b_candidate_raw_body": "not 1",
                "method_a_left_boundary": 0,
                "method_b_left_boundary": 0,
                "method_a_right_boundary": 5,
                "method_b_right_boundary": 5,
                "method_b_contamination": "clean",
            },
            {
                "id": "missing",
                "action_type": "creation",
                "method_b_contamination": "detected",
            },
            {
                "id": "unknown",
                "action_type": "creation",
                "method_a_candidate_raw_body": "not a",
                "method_b_candidate_raw_body": "a",
                "method_b_contamination": "unknown",
            },
        ]
    )
    overall = report["overall_agreement"]
    assert overall["raw_body_comparable_count"] == 2
    assert overall["raw_body_agreement_count"] == 1
    assert overall["raw_body_agreement_rate"] == 0.5
    assert overall["visible_text_comparable_count"] == 2
    assert overall["left_boundary_comparable_count"] == 1
    assert overall["left_boundary_agreement_count"] == 1
    assert overall["right_boundary_comparable_count"] == 1
    assert overall["critical_token_comparable_count"] == 2
    assert overall["critical_token_difference_count"] == 1
    assert overall["contamination_evaluated_count"] == 2
    assert overall["contamination_detected_count"] == 1
    assert overall["contamination_unknown_count"] == 1
    lifecycle = report["lifecycle_yields"][0]
    controls = report["method_a_safe_controls"]
    assert lifecycle["raw_body_comparable_count"] == 2
    assert controls["raw_body_comparable_count"] == 0
    assert controls["raw_body_agreement_rate"] is None


def test_recovery_accounting_and_blinded_packet() -> None:
    report = recovery_report(ROWS)
    assert report["input_count"] == 3 and report["recovered_count"] == 1
    packet = blinded_audit_packet(ROWS, seed=9)
    assert packet == blinded_audit_packet(ROWS, seed=9)
    assert "method_a_status" not in str(packet["reviewer_rows"])
    assert "b_safe" not in str(packet["reviewer_rows"])
    assert "strata" not in packet["reviewer_rows"][0]
    labels = {
        packet["reviewer_rows"][0]["candidate_1_label"],
        packet["reviewer_rows"][0]["candidate_2_label"],
    }
    assert labels == {"Candidate 1", "Candidate 2"}
    assert len(packet["reviewer_rows"]) == len(packet["unblinding_key"])


def test_localization_comparison_requires_same_frozen_pilot_set() -> None:
    before = [
        {
            "source_row_uid": "one",
            "method_a_status": "fallback",
            "method_b_status": "b_no_candidate",
            "assignment_status": "ambiguous",
            "candidate_count": 100,
            "assignment_conflicts": '["revision_too_large_for_safe_global_assignment"]',
        }
    ]
    after = [
        {
            "source_row_uid": "one",
            "method_a_status": "fallback",
            "status": "b_review",
            "assignment_status": "assigned",
            "whole_page_candidate_count": 100,
            "localized_candidate_count": 1,
            "neighboring_comment_contamination": "clean",
        }
    ]
    validation = {
        "overall_agreement": {
            "raw_body_comparable_count": 1,
            "raw_body_agreement_count": 1,
            "raw_body_agreement_rate": 1.0,
        },
        "lifecycle_yields": [],
    }
    report = localization_fix_comparison_report(
        before, after, validation_report=validation, expected_rows=1
    )
    assert report["same_source_row_uid_set"] == "PASS"
    assert report["assignment_limits_changed"] is False
    assert report["before"]["ambiguity_reasons"] == {
        "revision_too_large_for_safe_global_assignment": 1
    }
    assert report["after"]["candidate_distributions"]["whole_page"]["max"] == 100
    assert report["after"]["candidate_distributions"]["localized"]["max"] == 1
    assert report["after"]["contamination"] == {
        "evaluated": 1,
        "detected": 0,
        "unknown": 0,
    }
