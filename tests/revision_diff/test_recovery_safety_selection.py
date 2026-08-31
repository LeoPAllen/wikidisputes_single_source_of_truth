from __future__ import annotations

import json
from dataclasses import replace

from wikidisputes_ssot.revision_diff.assignment import AssignmentConfig
from wikidisputes_ssot.revision_diff.boundaries import extract_comment_candidates
from wikidisputes_ssot.revision_diff.models import (
    RevisionAvailability,
    RevisionText,
    local_content_sha256,
)
from wikidisputes_ssot.revision_diff.recovery import (
    neighboring_comment_contamination_status,
    recover_revision_actions,
)
from wikidisputes_ssot.revision_diff.safety import (
    SOFT_USABILITY_REASONS,
    assess_method_b_safety,
)
from wikidisputes_ssot.revision_diff.workflow import monotonic_selection_row


def _action(action_type: str, source_text: str, *, uid: str = "action-1") -> dict[str, object]:
    return {
        "action_uid": uid,
        "source_occurrence_uids": [f"source-{uid}"],
        "logical_utterance_uid": f"logical-{uid}",
        "action_id_exact": "2.10.10",
        "action_type": action_type,
        "source_text": source_text,
        "source_provenance_exact": True,
        "parentid_verified": True,
        "wikiconv_speaker": "Alice",
        "wikidisputes_speaker": "Alice source",
        "revision_actor": "Editor account",
    }


def _signed(body: str, user: str = "Alice") -> str:
    return f"{body} -- [[User:{user}]] 12:34, 1 January 2020 (UTC)"


def _page(bodies: list[str]) -> str:
    return "\n".join(_signed(body, f"User{index}") for index, body in enumerate(bodies))


def test_clean_addition_recovers_complete_comment() -> None:
    target = "== Topic ==\n\n" + _signed("New complete comment.")
    result = recover_revision_actions(
        [_action("creation", "New complete comment.")],
        RevisionText.available("1", "== Topic ==\n"),
        RevisionText.available("2", target),
    )[0]
    assert result.status == "b_safe"
    assert result.candidate_body == "New complete comment."
    assert result.candidate_raw == _signed("New complete comment.")


def test_small_modification_returns_full_target_comment_with_continuity() -> None:
    predecessor = _signed("The old detailed wording stays mostly intact.")
    target = _signed("The new detailed wording stays mostly intact.")
    result = recover_revision_actions(
        [_action("modification", "The new detailed wording stays mostly intact.")],
        RevisionText.available("1", predecessor),
        RevisionText.available("2", target),
    )[0]
    assert result.status == "b_safe"
    assert result.candidate_body == "The new detailed wording stays mostly intact."
    assert result.predecessor_target_continuity == "verified_structural_alignment"


def test_restoration_requires_and_uses_bounded_history_evidence() -> None:
    restored = _signed("Restored comment.")
    action = _action("restoration", "Restored comment.")
    action["restoration_history_body_hashes"] = [local_content_sha256("Restored comment.")]
    action["restoration_history_complete"] = True
    result = recover_revision_actions(
        [action], RevisionText.available("1", ""), RevisionText.available("2", restored)
    )[0]
    assert result.status == "b_safe"
    assert result.restoration_history_status == "verified"


def test_empty_defective_target_does_not_block_structurally_proven_candidate() -> None:
    action = _action("creation", "")
    result = recover_revision_actions(
        [action],
        RevisionText.available("1", ""),
        RevisionText.available("2", _signed("Archival content absent from target field.")),
    )[0]
    assert result.status == "b_safe"
    assert "target_coverage" not in result.reason_codes_json


def test_unavailable_predecessor_fails_closed() -> None:
    result = recover_revision_actions(
        [_action("creation", "New comment")],
        RevisionText("1", RevisionAvailability.UNAVAILABLE, None),
        RevisionText.available("2", _signed("New comment")),
    )[0]
    assert result.status == "b_unavailable"


def test_deletion_retains_predecessor_evidence_without_target_candidate() -> None:
    result = recover_revision_actions(
        [_action("deletion", "Removed comment")],
        RevisionText.available("1", _signed("Removed comment")),
        RevisionText.available("2", ""),
    )[0]
    assert result.status == "b_not_applicable"
    assert result.candidate_raw is None
    assert result.predecessor_candidate_body == "Removed comment"


def test_source_provenance_mismatch_cannot_be_safe() -> None:
    evidence = {
        "action_type": "creation",
        "target_availability": "available",
        "predecessor_availability": "available",
        "candidate_raw": "Candidate",
        "candidate_body": "Candidate",
        "source_provenance_exact": False,
        "revision_metadata_exact": True,
        "parentid_verified": True,
        "local_hashes_verified": True,
        "deterministic_diff_available": True,
        "changed_span_in_single_candidate": True,
        "assignment_unique": True,
        "assignment_uncontested": True,
        "lifecycle_consistent": True,
        "boundary_defensible": True,
    }
    decision = assess_method_b_safety(evidence)
    assert decision.status == "b_review"
    assert "source_provenance_mismatch" in decision.reason_codes


def test_signature_residue_only_is_usable_but_not_safe() -> None:
    evidence = {
        "action_type": "creation",
        "target_availability": "available",
        "predecessor_availability": "available",
        "candidate_raw": "Candidate",
        "candidate_body": "Candidate",
        "source_provenance_exact": True,
        "revision_metadata_exact": True,
        "parentid_verified": True,
        "local_hashes_verified": True,
        "deterministic_diff_available": True,
        "changed_span_in_single_candidate": True,
        "assignment_unique": True,
        "assignment_uncontested": True,
        "lifecycle_consistent": True,
        "boundary_defensible": True,
        "structural_warnings": ["terminal_signature"],
    }
    decision = assess_method_b_safety(evidence)
    assert decision.status == "b_usable"
    assert decision.safe is False
    assert set(decision.reason_codes).issubset(SOFT_USABILITY_REASONS)


def test_soft_signature_residue_cannot_mask_a_hard_reason() -> None:
    decision = assess_method_b_safety(
        {
            "action_type": "creation",
            "target_availability": "available",
            "predecessor_availability": "available",
            "candidate_raw": "Candidate",
            "candidate_body": "Candidate",
            "source_provenance_exact": False,
            "revision_metadata_exact": True,
            "parentid_verified": True,
            "local_hashes_verified": True,
            "deterministic_diff_available": True,
            "changed_span_in_single_candidate": True,
            "assignment_unique": True,
            "assignment_uncontested": True,
            "lifecycle_consistent": True,
            "boundary_defensible": True,
            "structural_warnings": ["terminal_signature"],
        }
    )
    assert decision.status == "b_review"


def test_revision_actor_signature_and_frozen_speakers_remain_separate() -> None:
    result = recover_revision_actions(
        [_action("creation", "Words")],
        RevisionText.available("1", ""),
        RevisionText.available("2", _signed("Words", user="Signature author")),
    )[0]
    assert result.revision_actor == "Editor account"
    assert result.signature_author == "Signature author"
    assert result.wikiconv_speaker == "Alice"
    assert result.wikidisputes_speaker == "Alice source"


def test_critical_contradiction_vetoes_only_aligned_informative_fragment() -> None:
    base = {
        "action_type": "creation",
        "target_availability": "available",
        "predecessor_availability": "available",
        "candidate_raw": "It is allowed.",
        "candidate_body": "It is allowed.",
        "source_provenance_exact": True,
        "revision_metadata_exact": True,
        "parentid_verified": True,
        "local_hashes_verified": True,
        "deterministic_diff_available": True,
        "changed_span_in_single_candidate": True,
        "assignment_unique": True,
        "assignment_uncontested": True,
        "lifecycle_consistent": True,
        "boundary_defensible": True,
        "signature_expected": True,
        "signature_timestamp_consistent": True,
    }
    unaligned = assess_method_b_safety(
        {
            **base,
            "informative_fragment": "It is not allowed.",
            "informative_fragment_aligned": False,
        }
    )
    aligned = assess_method_b_safety(
        {
            **base,
            "informative_fragment": "It is not allowed.",
            "aligned_candidate_fragment": "It is allowed.",
            "informative_fragment_aligned": True,
        }
    )
    assert unaligned.status == "b_safe"
    assert aligned.status == "b_review"
    assert "critical_token_contradiction_missing" in aligned.reason_codes


def test_monotonic_selection_keeps_method_a_safe_bytes_and_rejects_unsafe_b() -> None:
    a_safe = {
        "source_row_uid": "one",
        "method_a_status": "promote",
        "method_a_selected_text": "A exact bytes",
    }
    selected = monotonic_selection_row(a_safe, {"status": "b_safe", "candidate_body": "B"})
    assert selected["selected_method"] == "method_a"
    assert selected["selected_text"] == "A exact bytes"

    fallback = {
        "source_row_uid": "two",
        "method_a_status": "fallback",
        "method_a_selected_text": "trusted fallback",
    }
    unsafe = monotonic_selection_row(fallback, {"status": "b_review", "candidate_body": "unsafe"})
    usable = monotonic_selection_row(fallback, {"status": "b_usable", "candidate_body": "usable"})
    safe = monotonic_selection_row(fallback, {"status": "b_safe", "candidate_body": "safe"})
    assert unsafe["selected_text"] == "trusted fallback"
    assert usable["selected_method"] == "method_a_fallback"
    assert usable["selected_text"] == "trusted fallback"
    assert safe["selected_method"] == "method_b"
    assert safe["selected_text"] == "safe"


def test_hundred_comment_page_localizes_one_changed_comment_before_assignment() -> None:
    predecessor_bodies = [f"Comment {index}." for index in range(100)]
    target_bodies = predecessor_bodies.copy()
    target_bodies[50] = "Comment 50 changed."
    action = _action("modification", target_bodies[50])
    action["wikiconv_speaker"] = "User50"

    result = recover_revision_actions(
        [action],
        RevisionText.available("1", _page(predecessor_bodies)),
        RevisionText.available("2", _page(target_bodies)),
    )[0]

    assert result.whole_page_candidate_count == 100
    assert result.localized_candidate_count == 1
    assert result.assignment_status == "assigned"
    assert "revision_too_large_for_safe_global_assignment" not in result.assignment_conflicts_json


def test_many_page_comments_with_two_changes_assign_only_relevant_candidates() -> None:
    predecessor_bodies = [f"Comment {index}." for index in range(40)]
    target_bodies = predecessor_bodies.copy()
    target_bodies[5] = "Changed comment five."
    target_bodies[31] = "Changed comment thirty one."
    first = _action("modification", target_bodies[5], uid="first")
    second = _action("modification", target_bodies[31], uid="second")
    first["wikiconv_speaker"] = "User5"
    second["wikiconv_speaker"] = "User31"

    results = recover_revision_actions(
        [first, second],
        RevisionText.available("1", _page(predecessor_bodies)),
        RevisionText.available("2", _page(target_bodies)),
    )
    by_action = {result.action_uid: result for result in results}

    assert {result.whole_page_candidate_count for result in results} == {40}
    assert {result.localized_candidate_count for result in results} == {2}
    assert {result.assignment_status for result in results} == {"assigned"}
    assert by_action["first"].candidate_body.strip() == target_bodies[5]
    assert by_action["second"].candidate_body.strip() == target_bodies[31]
    assert all(len(json.loads(result.competing_candidates_json)) == 1 for result in results)


def test_single_action_with_one_localized_candidate_assigns_uniquely() -> None:
    result = recover_revision_actions(
        [_action("creation", "Only changed comment.")],
        RevisionText.available("1", ""),
        RevisionText.available("2", _signed("Only changed comment.")),
    )[0]
    assert result.localized_candidate_count == 1
    assert result.assignment_status == "assigned"
    assert json.loads(result.assignment_reason_codes_json) == ["unique_global_assignment"]


def test_tied_multi_action_evidence_remains_ambiguous() -> None:
    target = "\n".join((_signed("Same words.", "A"), _signed("Same words.", "B")))
    first = _action("creation", "Same words.", uid="first")
    second = _action("creation", "Same words.", uid="second")
    first["wikiconv_speaker"] = None
    second["wikiconv_speaker"] = None
    results = recover_revision_actions(
        [first, second], RevisionText.available("1", ""), RevisionText.available("2", target)
    )
    assert {result.assignment_status for result in results} == {"ambiguous"}
    assert all("equal_" in result.assignment_conflicts_json for result in results)


def test_boundary_whitespace_insertion_retains_only_structural_neighbors() -> None:
    first = _signed("First.", "A")
    second = _signed("Second.", "B")
    result = recover_revision_actions(
        [_action("modification", "First.")],
        RevisionText.available("1", first + second),
        RevisionText.available("2", first + "\n" + second),
    )[0]
    assert result.whole_page_candidate_count == 2
    assert result.localized_candidate_count == 2
    reasons = json.loads(result.localization_evidence_json)
    assert {item["candidate_uid"] for item in reasons} == {
        candidate.candidate_uid for candidate in extract_comment_candidates(first + "\n" + second)
    }
    assert all(
        "immediate_structural_neighbor_at_ambiguous_boundary" in item["reasons"] for item in reasons
    )


def test_genuinely_huge_localized_graph_keeps_existing_resource_guard() -> None:
    target = _page([f"New comment {index}." for index in range(31)])
    result = recover_revision_actions(
        [_action("creation", "")],
        RevisionText.available("1", ""),
        RevisionText.available("2", target),
        assignment_config=AssignmentConfig(),
    )[0]
    assert result.localized_candidate_count == 31
    assert result.assignment_status == "ambiguous"
    assert "revision_too_large_for_safe_global_assignment" in result.assignment_conflicts_json


def test_contamination_clean_detected_and_unknown_states() -> None:
    raw = "\n".join((_signed("First.", "A"), _signed("Second.", "B")))
    first, second = extract_comment_candidates(raw)
    assert neighboring_comment_contamination_status(first, [first, second], raw) == "clean"

    absorbed = replace(
        first,
        end=second.end,
        raw_wikitext=raw[first.start : second.end],
        body_end=second.body_end,
        body_wikitext=raw[first.start : second.body_end],
    )
    assert neighboring_comment_contamination_status(absorbed, [absorbed, second], raw) == "detected"

    indeterminate = replace(first, boundary_warnings=("uncertain_boundary",))
    assert (
        neighboring_comment_contamination_status(indeterminate, [indeterminate, second], raw)
        == "unknown"
    )


def test_unsigned_diff_span_fallback_requires_no_parsed_target_candidate() -> None:
    result = recover_revision_actions(
        [_action("creation", "Unsigned changed prose.")],
        RevisionText.available("1", ""),
        RevisionText.available("2", "Unsigned changed prose."),
    )[0]

    assert result.status == "b_safe"
    assert result.boundary_method == "diff_span_structural"
    assert result.candidate_raw == "Unsigned changed prose."
    assert result.candidate_body == result.candidate_raw
    assert result.neighboring_comment_contamination == "clean"
    assert "exact_source_body_corroboration" in result.assignment_evidence_json


def test_existing_parsed_candidate_precedes_diff_span_fallback() -> None:
    signed = _signed("Parsed target comment.")
    result = recover_revision_actions(
        [_action("creation", "Parsed target comment.")],
        RevisionText.available("1", ""),
        RevisionText.available("2", signed),
    )[0]

    assert result.boundary_method != "diff_span_structural"
    assert result.candidate_raw == signed
    assert result.signature_author == "Alice"


def test_unsigned_fallback_requires_all_global_target_spans() -> None:
    target = "First changed.\n\nSecond changed."
    result = recover_revision_actions(
        [_action("creation", target)],
        RevisionText.available("1", "\n\n"),
        RevisionText.available("2", target),
    )[0]

    assert result.boundary_method is None
    assert result.candidate_raw is None
    assert result.status != "b_safe"


def test_unsigned_fallback_source_mismatch_and_overwide_raw_are_not_safe() -> None:
    target = "Extra surrounding prose.\nChanged prose."
    result = recover_revision_actions(
        [_action("creation", "Changed prose.")],
        RevisionText.available("1", "Extra surrounding prose.\n"),
        RevisionText.available("2", target),
    )[0]

    assert result.boundary_method == "diff_span_structural"
    assert result.candidate_raw == target
    assert result.candidate_body == target
    assert result.status != "b_safe"
    assert result.neighboring_comment_contamination == "unknown"
    assert "exact_source_body_corroboration" not in result.assignment_evidence_json


def test_unsigned_fallback_with_one_inside_and_one_outside_span_fails_closed() -> None:
    target = _signed("Changed first.") + "\nChanged second."
    predecessor = _signed("Old first.") + "\n"
    result = recover_revision_actions(
        [_action("creation", "Changed first.")],
        RevisionText.available("1", predecessor),
        RevisionText.available("2", target),
    )[0]

    assert result.candidate_raw == _signed("Changed first.")
    assert result.status != "b_safe"
    assert "changed_span_not_in_one_comment" in result.reason_codes_json
