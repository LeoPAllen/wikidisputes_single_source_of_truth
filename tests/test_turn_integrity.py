from __future__ import annotations

import json

from wikidisputes_ssot.turn_integrity import (
    _git_state,
    decide_candidate,
    derived_turn_id,
    gold_status,
    placement_disposition,
    split_units,
    stable_case_id,
    structural_nonconversation,
)


def _candidate(kind: str, **extra: object) -> dict[str, object]:
    return {
        "source_dispute_id": "discussion-1",
        "source_row_uid": "row-1",
        "logical_utterance_uid": "logical-1",
        "problem_type": kind,
        "detector_evidence": {},
        **extra,
    }


def test_safe_split_requires_complete_historical_boundaries() -> None:
    parts = [
        {"text": "First", "source_revision_id": "10", "source_span": [0, 5]},
        {"text": "Second", "source_revision_id": "10", "source_span": [5, 11]},
    ]
    decision = decide_candidate(
        _candidate(
            "absorbed_multi_turn",
            detector_evidence={"boundary_status": "defensible", "parts": parts},
        )
    )
    assert decision["final_disposition"] == "split"
    assert [row["part_index"] for row in json.loads(str(decision["derived_units_json"]))] == [1, 2]


def test_ambiguous_split_suppresses_cumulative_block_and_multiple_timestamps_do_not_split() -> None:
    for provisional, severity in (("needs_history", "moderate"), ("repairable", "high")):
        decision = decide_candidate(
            _candidate(
                "absorbed_multi_turn",
                provisional_disposition=provisional,
                severity=severity,
                detector_evidence={"emitted_utc_marker_count": 2, "boundary_status": "contested"},
            )
        )
        assert decision["final_disposition"] == "row_exclude"
        assert decision["exclusion_reason"] == "cumulative_representation_unsafe"
    moderate_nomination = decide_candidate(
        _candidate(
            "absorbed_multi_turn",
            provisional_disposition="repairable",
            severity="moderate",
            detector_evidence={"emitted_utc_marker_count": 2},
        )
    )
    assert moderate_nomination["final_disposition"] == "keep"
    assert split_units("row-1", [], boundary_defensible=False) == []


def test_reviewed_cumulative_turn_suppresses_unsafe_block_even_when_detector_tier_is_modest() -> (
    None
):
    decision = decide_candidate(
        _candidate(
            "absorbed_multi_turn",
            dispute_sequence="D05016",
            provisional_disposition="repairable",
            severity="moderate",
            detector_evidence={
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_containment",
            },
        )
    )
    assert decision["final_disposition"] == "row_exclude"
    assert decision["exclusion_reason"] == "cumulative_representation_unsafe"


def test_lifecycle_proven_alias_and_genuine_repeated_post_are_distinct() -> None:
    alias = decide_candidate(
        _candidate("lifecycle_replay", detector_evidence={"lifecycle_identity": "proven_alias"})
    )
    repeated = decide_candidate(_candidate("lifecycle_replay", provisional_disposition="keep"))
    assert alias["final_disposition"] == "alias_or_suppress_duplicate"
    assert repeated["final_disposition"] == "row_exclude"
    # A documented genuinely-posted-twice case is not a replay candidate.
    assert decide_candidate(_candidate("genuine_repeated_post"))["final_disposition"] == "keep"


def test_cross_speaker_duplicates_require_lifecycle_evidence_not_text_similarity() -> None:
    ambiguous = decide_candidate(
        _candidate(
            "lifecycle_replay",
            dispute_sequence="D05862",
            detector_evidence={"same_text_cross_speaker": True},
        )
    )
    alias = decide_candidate(
        _candidate("lifecycle_replay", detector_evidence={"lifecycle_identity": "proven_alias"})
    )
    repost = decide_candidate(
        _candidate("lifecycle_replay", detector_evidence={"lifecycle_identity": "proven_repost"})
    )
    assert (ambiguous["final_disposition"], ambiguous["exclusion_reason"]) == (
        "row_exclude",
        "replay_representation_unsafe",
    )
    assert alias["final_disposition"] == "alias_or_suppress_duplicate"
    assert repost["final_disposition"] == "keep"


def test_structural_exclusion_keeps_meaningful_heading() -> None:
    structural = decide_candidate(
        _candidate(
            "formatting_or_empty", text="{|\n|}", detector_evidence={"structural_proven": True}
        )
    )
    heading = decide_candidate(
        _candidate(
            "formatting_or_empty",
            text="== A meaningful topic ==",
            detector_evidence={"structural_proven": True},
        )
    )
    assert structural["final_disposition"] == "row_exclude"
    assert heading["final_disposition"] == "keep"
    assert not structural_nonconversation(
        "== A meaningful topic ==", history_proves_structural=True
    )


def test_recoverable_and_irrecoverable_blank_text_are_not_conflated() -> None:
    recovered = decide_candidate(
        _candidate("formatting_or_empty", text="", detector_evidence={"recoverable_text": True})
    )
    lost = decide_candidate(
        _candidate(
            "formatting_or_empty", text="", detector_evidence={"annotation_text_blank": True}
        )
    )
    assert recovered["final_disposition"] == "recover"
    assert lost["final_disposition"] == "row_exclude"
    assert lost["exclusion_reason"] == "wikidisputes_method_a_text_unavailable"


def test_raw_wikidisputes_text_recovers_an_otherwise_blank_candidate() -> None:
    decision = decide_candidate(
        _candidate(
            "formatting_or_empty",
            text="",
            detector_evidence={"annotation_text_blank": True},
            raw_blank_fallback_candidate=True,
            wikidisputes_raw_record_found=True,
            wikidisputes_raw_text_exact="Exact source-record Method-A text",
            source_projection_sha256="projection-hash",
        )
    )

    assert decision["final_disposition"] == "wikidisputes_fallback"
    assert decision["fallback_text"] == "Exact source-record Method-A text"
    assert decision["fallback_text_source"] == "source_record_json_exact.text"
    evidence = json.loads(str(decision["evidence_json"]))
    assert evidence["reconstruction_rejected"] is True
    assert evidence["fallback_source_projection_sha256"] == "projection-hash"


def test_blank_raw_record_does_not_fall_back_to_a_derived_source_field() -> None:
    decision = decide_candidate(
        _candidate(
            "formatting_or_empty",
            text="",
            detector_evidence={"annotation_text_blank": True},
            wikidisputes_raw_record_found=True,
            wikidisputes_raw_text_exact="",
            wikidisputes_text_exact="later representation text",
        )
    )

    assert decision["final_disposition"] == "row_exclude"
    assert decision["exclusion_reason"] == "wikidisputes_method_a_text_unavailable"


def test_actor_signature_review_preserves_proven_source_and_drops_attribution_stub() -> None:
    verified = decide_candidate(
        _candidate(
            "actor_signature_verified",
            dispute_sequence="D01057",
            detector_evidence={"actor_signature_status": "proven_match"},
        )
    )
    stub = decide_candidate(
        _candidate(
            "formatting_or_empty",
            dispute_sequence="D08584",
            text="",
            detector_evidence={"structural_proven": True, "annotation_text_blank": True},
        )
    )
    assert verified["final_disposition"] == "keep"
    assert (stub["final_disposition"], stub["exclusion_reason"]) == (
        "row_exclude",
        "structural_nonconversation",
    )


def test_fragment_decisions_require_direct_neighbor_or_structural_evidence() -> None:
    reattached = decide_candidate(
        _candidate(
            "fragmentary_row",
            detector_evidence={
                "reattach_target_source_uid": "previous-source",
                "append_text": "tail",
                "review_basis": "immediate_source_neighbor_same_user_timestamp",
            },
        )
    )
    structural = decide_candidate(
        _candidate(
            "fragmentary_row",
            text="''",
            detector_evidence={"structural_proven": True, "review_basis": "markup_only"},
        )
    )
    assert (reattached["final_disposition"], reattached["exclusion_reason"]) == (
        "alias_or_suppress_duplicate",
        "fragment_reattached",
    )
    assert (structural["final_disposition"], structural["exclusion_reason"]) == (
        "row_exclude",
        "structural_nonconversation",
    )


def test_fragment_can_restore_its_own_authoritative_revision_comment() -> None:
    recovered = decide_candidate(
        _candidate(
            "fragmentary_row",
            detector_evidence={
                "recovered_annotation_text": (
                    "And bringing nonsense into the opening ... doesn't help it at all."
                ),
                "review_basis": "authoritative_revision_physical_comment",
            },
        )
    )
    assert recovered["final_disposition"] == "recover"
    assert recovered["fallback_text"] == (
        "And bringing nonsense into the opening ... doesn't help it at all."
    )
    assert recovered["fallback_text_source"].endswith("current_annotation_text")


def test_d01057_restore_sequence_splits_three_signed_turns_in_order() -> None:
    parts = [
        {
            "text": "first",
            "speaker_id": "Still-24-45-42-125",
            "source_revision_id": "504534524",
            "source_span": [0, 338],
            "creation_evidence": "revision_restore_sequence",
        },
        {
            "text": "second",
            "speaker_id": "Belchfire",
            "source_revision_id": "504534524",
            "source_span": [339, 421],
            "creation_evidence": "revision_restore_sequence",
        },
        {
            "text": "third",
            "speaker_id": "Still-24-45-42-125",
            "source_revision_id": "504534524",
            "source_span": [422, 662],
            "creation_evidence": "revision_restore_sequence",
        },
    ]
    decision = decide_candidate(
        _candidate(
            "absorbed_multi_turn",
            dispute_sequence="D01057",
            detector_evidence={"boundary_status": "defensible", "parts": parts},
        )
    )
    units = json.loads(str(decision["derived_units_json"]))
    assert decision["final_disposition"] == "split"
    assert [(unit["part_index"], unit["speaker_id"], unit["source_span"]) for unit in units] == [
        (1, "Still-24-45-42-125", [0, 338]),
        (2, "Belchfire", [339, 421]),
        (3, "Still-24-45-42-125", [422, 662]),
    ]


def test_missing_time_safe_and_ambiguous_placement() -> None:
    assert placement_disposition(verified_creation=False, feasible_positions=[3]) == ("keep", None)
    assert placement_disposition(verified_creation=False, feasible_positions=[2, 3]) == (
        "dispute_exclude",
        "trajectory_position_ambiguous",
    )


def test_ids_are_deterministic() -> None:
    assert stable_case_id("d", "r", "x", "l") == stable_case_id("d", "r", "x", "l")
    assert derived_turn_id("r", 1) == derived_turn_id("r", 1)
    assert derived_turn_id("r", 1) != derived_turn_id("r", 2)


def test_d16_and_d31_gold_policy() -> None:
    d16 = decide_candidate(_candidate("absorbed_multi_turn", dispute_sequence="D16"))
    d31 = decide_candidate(_candidate("lifecycle_replay", dispute_sequence="D31"))
    assert d16["final_disposition"] == "row_exclude"
    assert d16["exclusion_reason"] == "cumulative_representation_unsafe"
    assert gold_status(str(d16["final_disposition"])) == "invalidated"
    assert d31["final_disposition"] == "alias_or_suppress_duplicate"
    assert gold_status(str(d31["final_disposition"])) == "needs_rereview"


def test_git_state_preserves_short_status_columns(monkeypatch, tmp_path) -> None:
    outputs = iter(
        [
            "feature/utterance_fidelity\n",
            "abc123\n",
            " M tracked.txt\n?? untracked.txt\n",
        ]
    )

    class Result:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    monkeypatch.setattr(
        "wikidisputes_ssot.turn_integrity.subprocess.run",
        lambda *args, **kwargs: Result(next(outputs)),
    )

    state = _git_state(tmp_path)
    assert state == {
        "branch": "feature/utterance_fidelity",
        "head": "abc123",
        "status_short": " M tracked.txt\n?? untracked.txt",
    }
