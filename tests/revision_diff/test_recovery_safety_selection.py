from __future__ import annotations

from wikidisputes_ssot.revision_diff.models import (
    RevisionAvailability,
    RevisionText,
    local_content_sha256,
)
from wikidisputes_ssot.revision_diff.recovery import recover_revision_actions
from wikidisputes_ssot.revision_diff.safety import assess_method_b_safety
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
    unsafe = monotonic_selection_row(
        fallback, {"status": "b_review", "candidate_body": "unsafe"}
    )
    safe = monotonic_selection_row(fallback, {"status": "b_safe", "candidate_body": "safe"})
    assert unsafe["selected_text"] == "trusted fallback"
    assert safe["selected_method"] == "method_b"
    assert safe["selected_text"] == "safe"
