from wikidisputes_ssot.revision_diff.reporting import (
    blinded_audit_packet,
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
