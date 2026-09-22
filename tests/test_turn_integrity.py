from __future__ import annotations

import json

from wikidisputes_ssot.turn_integrity import (
    _candidate_detection_text,
    _discover_population_candidates,
    _git_state,
    _resolve_high_confidence_replay_bundles,
    _resolve_longitudinal_replays,
    _staged_or_source_text,
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
    assert (repeated["final_disposition"], repeated["decision_reason"]) == (
        "keep",
        "unresolved_replay_identity",
    )
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
    assert (ambiguous["final_disposition"], ambiguous["decision_reason"]) == (
        "keep",
        "unresolved_replay_identity",
    )
    assert alias["final_disposition"] == "alias_or_suppress_duplicate"
    assert repost["final_disposition"] == "keep"


def test_population_candidates_are_episode_scoped_and_evidence_only() -> None:
    replay = "A" * 600
    near_replay = (("word " * 75) + " word " + ("word " * 74)).strip()
    contained = "C" * 150
    units = [
        {
            "source_row_uid": "exact-1",
            "source_dispute_id": "d1",
            "episode_uid": "episode-1",
            "source_order": 1,
            "speaker_id": "one",
            "text": replay,
        },
        {
            "source_row_uid": "exact-2",
            "source_dispute_id": "d1",
            "episode_uid": "episode-1",
            "source_order": 2,
            "speaker_id": "two",
            "text": replay,
        },
        {
            "source_row_uid": "near-1",
            "source_dispute_id": "d1",
            "episode_uid": "episode-1",
            "source_order": 3,
            "speaker_id": "one",
            "text": ("word " * 150).strip(),
        },
        {
            "source_row_uid": "near-2",
            "source_dispute_id": "d1",
            "episode_uid": "episode-1",
            "source_order": 4,
            "speaker_id": "two",
            "text": near_replay,
        },
        {
            "source_row_uid": "short-turn",
            "source_dispute_id": "d1",
            "episode_uid": "episode-1",
            "source_order": 5,
            "speaker_id": "three",
            "text": contained,
        },
        {
            "source_row_uid": "cumulative",
            "source_dispute_id": "d1",
            "episode_uid": "episode-1",
            "source_order": 6,
            "speaker_id": "four",
            "text": ("prefix " * 30) + contained + (" suffix" * 30),
        },
        {
            "source_row_uid": "residue",
            "source_dispute_id": "d1",
            "episode_uid": "episode-1",
            "source_order": 7,
            "speaker_id": "four",
            "text": "''",
        },
        # Identical text in another episode is deliberately not a replay.
        {
            "source_row_uid": "other-episode",
            "source_dispute_id": "d2",
            "episode_uid": "episode-2",
            "source_order": 1,
            "speaker_id": "five",
            "text": replay,
        },
    ]

    candidates = _discover_population_candidates(units)
    by_type = {}
    for candidate in candidates:
        by_type.setdefault(candidate["problem_type"], []).append(candidate)
    assert {row["source_row_uid"] for row in by_type["exact_replay"]} == {"exact-1", "exact-2"}
    assert {row["source_row_uid"] for row in by_type["near_replay"]} == {"near-1", "near-2"}
    [cumulative_case] = [
        row for row in by_type["absorbed_multi_turn"] if row["source_row_uid"] == "cumulative"
    ]
    assert cumulative_case["detector_evidence"]["contained_source_row_uids"] == ["short-turn"]
    assert decide_candidate(cumulative_case)["final_disposition"] == "row_exclude"
    [fragment_case] = [
        row for row in by_type["fragmentary_row"] if row["source_row_uid"] == "residue"
    ]
    assert decide_candidate(fragment_case)["exclusion_reason"] == "structural_nonconversation"
    replay_decision = decide_candidate(by_type["exact_replay"][0])
    assert (replay_decision["final_disposition"], replay_decision["decision_reason"]) == (
        "keep",
        "unresolved_replay_identity",
    )


def test_high_confidence_replay_bundle_aliases_to_retained_earlier_anchors() -> None:
    rows = [
        _candidate(
            "lifecycle_replay",
            source_row_uid="later-a",
            source_dispute_id="d1",
            detector_evidence={
                "source": "normalized_repeat_revision_batch",
                "revision_prefix": "100",
                "current_timestamp": "2020-01-02T00:00:00+00:00",
                "anchor_utterance_id": "earlier-a",
            },
        ),
        _candidate(
            "lifecycle_replay",
            source_row_uid="later-b",
            source_dispute_id="d1",
            detector_evidence={
                "source": "normalized_repeat_revision_batch",
                "revision_prefix": "100",
                "current_timestamp": "2020-01-02T00:00:00+00:00",
                "anchor_utterance_id": "earlier-b",
            },
        ),
    ]
    _resolve_high_confidence_replay_bundles(
        rows, anchor_sources_by_utterance={"earlier-a": "anchor-a", "earlier-b": "anchor-b"}
    )
    decisions = [decide_candidate(row) for row in rows]
    assert [row["final_disposition"] for row in decisions] == [
        "alias_or_suppress_duplicate",
        "alias_or_suppress_duplicate",
    ]
    assert {
        json.loads(str(row["evidence_json"]))["anchor_source_row_uid"] for row in decisions
    } == {"anchor-a", "anchor-b"}


def test_replay_bundle_skips_an_excluded_anchor_and_resolves_an_alias_chain() -> None:
    rows = [
        _candidate(
            "lifecycle_replay",
            source_row_uid="later-excluded",
            source_dispute_id="d1",
            detector_evidence={
                "source": "normalized_repeat_revision_batch",
                "revision_prefix": "100",
                "current_timestamp": "2020-01-02T00:00:00+00:00",
                "anchor_utterance_id": "excluded",
                "lifecycle_identity": "proven_alias",
                "anchor_source_row_uid": "excluded-anchor",
                "replay_bundle": {"high_confidence": True},
            },
        ),
        _candidate(
            "lifecycle_replay",
            source_row_uid="later-chain",
            source_dispute_id="d1",
            detector_evidence={
                "source": "normalized_repeat_revision_batch",
                "revision_prefix": "100",
                "current_timestamp": "2020-01-02T00:00:00+00:00",
                "anchor_utterance_id": "middle",
            },
        ),
        _candidate(
            "fragmentary_row",
            source_row_uid="excluded-anchor",
            detector_evidence={"structural_proven": True},
            text="''",
        ),
        _candidate(
            "lifecycle_replay",
            source_row_uid="middle-anchor",
            detector_evidence={
                "lifecycle_identity": "proven_alias",
                "anchor_source_row_uid": "retained-anchor",
            },
        ),
    ]
    _resolve_high_confidence_replay_bundles(
        rows,
        anchor_sources_by_utterance={
            "excluded": "excluded-anchor",
            "middle": "middle-anchor",
        },
    )
    by_source = {str(row["source_row_uid"]): decide_candidate(row) for row in rows}
    assert by_source["later-excluded"]["final_disposition"] == "keep"
    chained = json.loads(str(by_source["later-chain"]["evidence_json"]))
    assert by_source["later-chain"]["final_disposition"] == "alias_or_suppress_duplicate"
    assert chained["anchor_source_row_uid"] == "retained-anchor"


def test_slot_replay_requires_coordinate_and_root_evidence() -> None:
    incomplete = decide_candidate(
        _candidate(
            "near_replay",
            detector_evidence={
                "physical_comment_slot": {
                    "stable_across_revisions": True,
                    "anchor_source_row_uid": "earlier",
                }
            },
        )
    )
    proven = decide_candidate(
        _candidate(
            "near_replay",
            detector_evidence={
                "physical_comment_slot": {
                    "stable_across_revisions": True,
                    "action_coordinate": "revision:50:offset:200",
                    "root_evidence": "wikiconv-root:1",
                    "anchor_source_row_uid": "earlier",
                    "anchor_existed_before_later_touch": True,
                }
            },
        )
    )
    assert incomplete["final_disposition"] == "keep"
    assert proven["final_disposition"] == "alias_or_suppress_duplicate"


def test_longitudinal_replay_requires_full_history_proof_and_keeps_reposts() -> None:
    rows = [
        _candidate(
            "exact_replay",
            source_row_uid="D01997-later",
            detector_evidence={
                "physical_comment_slot": {
                    "stable_across_revisions": True,
                    "action_coordinate": "revision:99:offset:12",
                    "root_evidence": "root:discussion:1",
                    "anchor_utterance_id": "D01997-earlier-id",
                    "anchor_existed_before_later_touch": True,
                }
            },
        ),
        _candidate(
            "exact_replay",
            source_row_uid="D01054-repost",
            detector_evidence={
                # Same text/cross-speaker evidence intentionally does not
                # stand in for an action coordinate and prior-existence fact.
                "same_text_cross_speaker": True,
                "physical_comment_slot": {"stable_across_revisions": True},
            },
        ),
        _candidate(
            "exact_replay",
            source_row_uid="D08854-later",
            detector_evidence={
                "physical_comment_slot": {
                    "stable_across_revisions": True,
                    "action_coordinate": "action:1224",
                    "root_evidence": "root:714",
                    "anchor_utterance_id": "D08854-earlier-id",
                    "anchor_existed_before_later_touch": True,
                }
            },
        ),
        _candidate(
            "exact_replay",
            source_row_uid="D08313-later",
            detector_evidence={
                "physical_comment_slot": {
                    "stable_across_revisions": True,
                    "action_coordinate": "action:50129",
                    "root_evidence": "root:0",
                    "anchor_utterance_id": "D08313-earlier-id",
                    "anchor_existed_before_later_touch": True,
                }
            },
        ),
        _candidate(
            "exact_replay",
            source_row_uid="D02194-repost",
            detector_evidence={
                "physical_comment_slot": {
                    "stable_across_revisions": True,
                    "action_coordinate": "revision:101:offset:12",
                    "root_evidence": "root:discussion:2",
                    "anchor_utterance_id": "D02194-earlier-id",
                    # This is a genuine later post, not a history proof.
                    "anchor_existed_before_later_touch": False,
                }
            },
        ),
    ]
    _resolve_longitudinal_replays(
        rows,
        anchor_sources_by_utterance={
            "D01997-earlier-id": "D01997-earlier",
            "D08854-earlier-id": "D08854-earlier",
            "D08313-earlier-id": "D08313-earlier",
            "D02194-earlier-id": "D02194-earlier",
        },
    )
    assert decide_candidate(rows[0])["final_disposition"] == "alias_or_suppress_duplicate"
    assert decide_candidate(rows[1])["final_disposition"] == "keep"
    assert decide_candidate(rows[2])["final_disposition"] == "alias_or_suppress_duplicate"
    assert decide_candidate(rows[3])["final_disposition"] == "alias_or_suppress_duplicate"
    assert decide_candidate(rows[4])["final_disposition"] == "keep"


def test_population_coordinates_resolve_named_positives_not_negative_controls() -> None:
    units = []
    fixtures = [
        ("D01997", "100.4826.4806", "101.4826.4806", True),
        ("D08313", "200.50129.0", "201.50129.0", True),
        ("D08854", "300.1224.714", "301.1224.714", True),
        # A self-root coordinate is not enough to establish carry-forward
        # identity; these can be independent source acts with repeated text.
        ("D02194", "400.11160.11160", "401.11160.11160", False),
        # Same text and root but a different action coordinate is likewise
        # not one physical comment slot.
        ("D01054", "500.27654.27588", "501.147107.27588", False),
    ]
    expected = {}
    for fixture, earlier_id, later_id, should_resolve in fixtures:
        text = (f"{fixture} physical comment " * 12).strip()
        units.extend(
            [
                {
                    "source_row_uid": f"{fixture}-earlier",
                    "source_dispute_id": fixture,
                    "source_order": 1,
                    "speaker_id": "Earlier",
                    "utterance_id": earlier_id,
                    "text": text,
                },
                {
                    "source_row_uid": f"{fixture}-later",
                    "source_dispute_id": fixture,
                    "source_order": 2,
                    "speaker_id": "Later",
                    "utterance_id": later_id,
                    "text": text,
                },
            ]
        )
        expected[f"{fixture}-later"] = should_resolve
    candidates = _discover_population_candidates(units)
    later_candidates = {
        row["source_row_uid"]: row
        for row in candidates
        if row["problem_type"] == "exact_replay" and str(row["source_row_uid"]).endswith("-later")
    }
    assert set(later_candidates) == set(expected)
    for source_uid, should_resolve in expected.items():
        decision = decide_candidate(later_candidates[source_uid])
        assert (decision["final_disposition"] == "alias_or_suppress_duplicate") is should_resolve
        evidence = json.loads(str(decision["evidence_json"]))
        if should_resolve:
            slot = evidence["physical_comment_slot"]
            assert slot["anchor_source_row_uid"] == source_uid.replace("-later", "-earlier")
            assert slot["anchor_revision_id"] < slot["later_revision_id"]
            assert slot["anchor_existed_before_later_touch"] is True
            assert slot["proof_source"] == "wikiconv_stable_nonroot_comment_coordinate"
        else:
            assert "physical_comment_slot" not in evidence


def test_short_exact_replays_need_adjacency_or_different_speakers() -> None:
    short = "exact replay " * 9  # 117 characters
    candidates = _discover_population_candidates(
        [
            {
                "source_row_uid": "a",
                "source_dispute_id": "d1",
                "source_order": 1,
                "speaker_id": "A",
                "text": short,
            },
            {
                "source_row_uid": "b",
                "source_dispute_id": "d1",
                "source_order": 2,
                "speaker_id": "A",
                "text": short,
            },
            {
                "source_row_uid": "c",
                "source_dispute_id": "d2",
                "source_order": 1,
                "speaker_id": "A",
                "text": short,
            },
            {
                "source_row_uid": "middle",
                "source_dispute_id": "d2",
                "source_order": 2,
                "speaker_id": "A",
                "text": "a distinct intervening contribution" * 4,
            },
            {
                "source_row_uid": "d",
                "source_dispute_id": "d2",
                "source_order": 3,
                "speaker_id": "A",
                "text": short,
            },
            {
                "source_row_uid": "e",
                "source_dispute_id": "d3",
                "source_order": 1,
                "speaker_id": "A",
                "text": short,
            },
            {
                "source_row_uid": "f",
                "source_dispute_id": "d3",
                "source_order": 4,
                "speaker_id": "B",
                "text": short,
            },
        ]
    )
    flagged = {row["source_row_uid"] for row in candidates if row["problem_type"] == "exact_replay"}
    assert flagged == {"a", "b", "e", "f"}


def test_high_similarity_replay_is_candidate_only_without_provenance() -> None:
    original = ("alpha beta gamma delta epsilon " * 24) + "original tail"
    revised = original.replace("gamma", "gammb", 1)
    candidates = _discover_population_candidates(
        [
            {
                "source_row_uid": "original",
                "source_dispute_id": "d1",
                "source_order": 1,
                "speaker_id": "A",
                "text": original,
            },
            {
                "source_row_uid": "revised",
                "source_dispute_id": "d1",
                "source_order": 2,
                "speaker_id": "A",
                "text": revised,
            },
        ]
    )
    replay_candidates = [row for row in candidates if row["problem_type"] == "near_replay"]
    assert {row["source_row_uid"] for row in replay_candidates} == {"original", "revised"}
    for candidate in replay_candidates:
        assert candidate["detector_evidence"]["candidate_only_signal"] is True
        decision = decide_candidate(candidate)
        assert decision["final_disposition"] == "keep"
        assert decision["decision_reason"] == "unresolved_replay_identity"


def test_high_similarity_cross_speaker_pair_need_not_be_adjacent() -> None:
    original = ("long cross speaker replay evidence " * 22) + "one ending"
    revised = original.replace("evidence", "evidencf", 1)
    candidates = _discover_population_candidates(
        [
            {
                "source_row_uid": "original",
                "source_dispute_id": "d1",
                "source_order": 1,
                "speaker_id": "A",
                "text": original,
            },
            {
                "source_row_uid": "intervening",
                "source_dispute_id": "d1",
                "source_order": 2,
                "speaker_id": "A",
                "text": "unrelated contribution " * 30,
            },
            {
                "source_row_uid": "revised",
                "source_dispute_id": "d1",
                "source_order": 3,
                "speaker_id": "B",
                "text": revised,
            },
        ]
    )
    assert {
        row["source_row_uid"] for row in candidates if row["problem_type"] == "near_replay"
    } == {"original", "revised"}


def test_clear_multiple_signature_boundaries_are_a_blocking_merge_candidate() -> None:
    text = (
        "First comment -- [[User:One|One]] 10:00, 1 January 2010 (UTC)\n"
        "Second comment -- [[User:Two|Two]] 10:01, 1 January 2010 (UTC)"
    )
    [candidate] = [
        row
        for row in _discover_population_candidates(
            [
                {
                    "source_row_uid": "merged",
                    "source_dispute_id": "d1",
                    "source_order": 1,
                    "text": text,
                }
            ]
        )
        if row["problem_type"] == "absorbed_multi_turn"
    ]
    decision = decide_candidate(candidate)
    assert candidate["detector_evidence"]["clear_signature_boundary_count"] == 2
    assert decision["final_disposition"] == "keep"
    assert decision["annotation_blocking"] is True
    assert decision["annotation_blocking_reason"] == "unresolved_high_confidence_merged_comment"


def test_named_composite_regressions_are_nominated_but_never_text_split() -> None:
    units = [
        {
            "source_row_uid": "D00070",
            "source_dispute_id": "D00070",
            "source_order": 1,
            "text": ("first contribution " * 20) + "'''''' |\n" + ("second " * 20) + "'''''' |",
        },
        {
            "source_row_uid": "D02859",
            "source_dispute_id": "D02859",
            "source_order": 1,
            "text": "history-backed merged contribution",
            "turn_integrity_provenance": {"merged_preceding_count": 1},
        },
        {
            "source_row_uid": "D03069",
            "source_dispute_id": "D03069",
            "source_order": 1,
            "text": "history could not localize this modification to one comment",
            "turn_integrity_provenance": {
                "action_type": "modification",
                "changed_span_not_in_one_comment": True,
            },
        },
        {
            "source_row_uid": "D03685",
            "source_dispute_id": "D03685",
            "source_order": 1,
            "speaker_id": "later editor",
            "text": "multiple unsigned contributions",
            "turn_integrity_provenance": {
                "merged_preceding_count": 2,
                "speaker_signature_provenance": "mismatch",
                "signature_author": "earlier signer",
                "single_contribution_proven": False,
            },
        },
        {
            "source_row_uid": "D05535",
            "source_dispute_id": "D05535",
            "source_order": 1,
            "text": "first 10:00, 1 January 2010 (UTC)\nsecond 10:01, 1 January 2010 (UTC)",
        },
        {
            "source_row_uid": "D08018",
            "source_dispute_id": "D08018",
            "source_order": 1,
            "text": "autosigned contribution followed by another contribution",
            "turn_integrity_provenance": {"merged_preceding_count": 1},
        },
    ]
    candidates = _discover_population_candidates(units)
    composites = {
        row["source_row_uid"]: row
        for row in candidates
        if row["problem_type"] == "absorbed_multi_turn"
    }
    assert set(composites) == {
        "D00070",
        "D02859",
        "D03069",
        "D03685",
        "D05535",
        "D08018",
    }
    assert not any(
        row["problem_type"] == "speaker_signature_conflict" and row["source_row_uid"] == "D03685"
        for row in candidates
    )
    for candidate in composites.values():
        decision = decide_candidate(candidate)
        assert decision["final_disposition"] == "keep"
        assert json.loads(str(decision["derived_units_json"])) == []
        assert decision["annotation_blocking"] is True


def test_explicit_signature_repairs_only_a_proven_single_contribution() -> None:
    [candidate] = _discover_population_candidates(
        [
            {
                "source_row_uid": "mismatch",
                "source_dispute_id": "d1",
                "source_order": 1,
                "speaker_id": "SineBot",
                "text": "one recovered contribution",
                "turn_integrity_provenance": {
                    "speaker_signature_provenance": "mismatch",
                    "signature_author": "ActualAuthor",
                    "single_contribution_proven": True,
                },
            }
        ]
    )
    decision = decide_candidate(candidate)
    evidence = json.loads(str(decision["evidence_json"]))
    assert decision["problem_type"] == "speaker_signature_conflict"
    assert decision["final_disposition"] == "keep"
    assert decision["decision_reason"] == "speaker_repaired_from_explicit_signature"
    assert decision["annotation_blocking"] is False
    assert evidence["speaker_replacement"] == "ActualAuthor"
    assert evidence["source_speaker_id"] == "SineBot"
    assert evidence["raw_provenance_preserved"] is True


def test_unresolved_explicit_signature_conflict_blocks_annotation() -> None:
    [candidate] = _discover_population_candidates(
        [
            {
                "source_row_uid": "mismatch",
                "source_dispute_id": "d1",
                "source_order": 1,
                "speaker_id": "LaterEditor",
                "text": "one unresolved contribution",
                "turn_integrity_provenance": {
                    "speaker_signature_provenance": "mismatch",
                    "signature_author": "EarlierAuthor",
                    "single_contribution_proven": False,
                },
            }
        ]
    )
    decision = decide_candidate(candidate)
    assert decision["decision_reason"] == "unresolved_speaker_signature_conflict"
    assert decision["annotation_blocking"] is True
    assert decision["annotation_blocking_reason"] == "unresolved_speaker_signature_conflict"


def test_resolved_high_confidence_composite_is_not_a_blocker() -> None:
    decision = decide_candidate(
        _candidate(
            "absorbed_multi_turn",
            detector_evidence={
                "detector_class": "multiple_clear_signature_boundaries",
                "merged_comment_confidence": "high",
                "constituent_turns_already_present": True,
            },
        )
    )
    assert decision["final_disposition"] == "row_exclude"
    assert decision["annotation_blocking"] is False


def test_generic_fragment_is_not_annotation_blocking() -> None:
    decision = decide_candidate(_candidate("fragmentary_row", detector_evidence={}))
    assert decision["annotation_blocking"] is False


def test_population_detection_falls_back_from_blank_staged_text_to_source_text() -> None:
    replay = "D00802 replay " * 130
    assert (
        _staged_or_source_text({"utterance_text": ""}, {"wikidisputes_text_exact": replay})
        == replay
    )


def test_population_replay_detector_accepts_adjacent_same_speaker_exact_and_tiny_near_only() -> (
    None
):
    exact = "exact replay " * 60
    near = "near replay " * 60
    candidates = _discover_population_candidates(
        [
            {
                "source_row_uid": "exact-a",
                "source_dispute_id": "d1",
                "source_order": 1,
                "speaker_id": "a",
                "text": exact,
            },
            {
                "source_row_uid": "exact-b",
                "source_dispute_id": "d1",
                "source_order": 2,
                "speaker_id": "a",
                "text": exact,
            },
            {
                "source_row_uid": "near-a",
                "source_dispute_id": "d2",
                "source_order": 1,
                "speaker_id": "a",
                "text": near,
            },
            {
                "source_row_uid": "near-b",
                "source_dispute_id": "d2",
                "source_order": 2,
                "speaker_id": "a",
                "text": "  " + near,
            },
            {
                "source_row_uid": "negative-a",
                "source_dispute_id": "d3",
                "source_order": 1,
                "speaker_id": "a",
                "text": "alpha " * 100,
            },
            {
                "source_row_uid": "negative-b",
                "source_dispute_id": "d3",
                "source_order": 2,
                "speaker_id": "a",
                "text": "beta " * 100,
            },
        ]
    )
    by_type = {}
    for candidate in candidates:
        by_type.setdefault(candidate["problem_type"], set()).add(candidate["source_row_uid"])
    assert by_type["exact_replay"] == {"exact-a", "exact-b"}
    assert by_type["near_replay"] == {"near-a", "near-b"}


def test_population_detection_discovers_blank_staged_source_replay() -> None:
    replay = "D00802 replay " * 130
    candidates = _discover_population_candidates(
        [
            {
                "source_row_uid": "yobol",
                "source_dispute_id": "d00802",
                "episode_uid": "episode-d00802",
                "source_order": 1,
                "speaker_id": "Yobol",
                "text": replay,
            },
            {
                "source_row_uid": "albinoferret",
                "source_dispute_id": "d00802",
                "episode_uid": "episode-d00802",
                "source_order": 2,
                "speaker_id": "AlbinoFerret",
                "text": replay,
            },
        ]
    )
    assert {
        row["source_row_uid"] for row in candidates if row["problem_type"] == "exact_replay"
    } == {
        "yobol",
        "albinoferret",
    }


def test_population_detection_uses_source_text_for_a_final_fallback_representation() -> None:
    source_text = "D00802 replay " * 130
    assert (
        _candidate_detection_text(
            {"utterance_text": source_text + " revised"},
            {"wikidisputes_text_exact": source_text},
            final_uses_source_text=True,
        )
        == source_text
    )


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
