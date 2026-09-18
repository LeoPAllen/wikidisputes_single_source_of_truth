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


def test_ambiguous_split_excludes_dispute_and_multiple_timestamps_do_not_split() -> None:
    for provisional, severity in (("needs_history", "moderate"), ("repairable", "high")):
        decision = decide_candidate(
            _candidate(
                "absorbed_multi_turn",
                provisional_disposition=provisional,
                severity=severity,
                detector_evidence={"emitted_utc_marker_count": 2, "boundary_status": "contested"},
            )
        )
        assert decision["final_disposition"] == "dispute_exclude"
        assert decision["exclusion_reason"] == "unsplittable_multi_turn"
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


def test_lifecycle_proven_alias_and_genuine_repeated_post_are_distinct() -> None:
    alias = decide_candidate(
        _candidate("lifecycle_replay", detector_evidence={"lifecycle_identity": "proven_alias"})
    )
    repeated = decide_candidate(_candidate("lifecycle_replay", provisional_disposition="keep"))
    assert alias["final_disposition"] == "alias_or_suppress_duplicate"
    assert repeated["final_disposition"] == "dispute_exclude"
    # A documented genuinely-posted-twice case is not a replay candidate.
    assert decide_candidate(_candidate("genuine_repeated_post"))["final_disposition"] == "keep"


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
    assert lost["exclusion_reason"] == "meaningful_text_unrecoverable"


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
    assert d16["exclusion_reason"] == "unsplittable_multi_turn"
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
