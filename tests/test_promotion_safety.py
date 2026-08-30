import pytest

from wikidisputes_ssot.promotion_safety import assess_promotion

HIGH_CONFIDENCE = {
    "recovery_status": "high_confidence",
    "target_coverage": 1.0,
    "candidate_purity": 1.0,
    "match_margin": 0.4,
    "signature_residue_detected": False,
}


def test_legitimate_wikilink_and_url_restoration_is_promoted() -> None:
    trusted = "See Neutral point of view and this source for the relevant policy."
    candidate = (
        "See [[Wikipedia:Neutral point of view|Neutral point of view]] and "
        "[https://example.test/source this source] for the relevant policy."
    )
    decision = assess_promotion(trusted, candidate, HIGH_CONFIDENCE)
    assert decision.decision == "promote"
    assert decision.ordered_token_retention == 1.0


def test_mikolaj_semantic_corruption_is_rejected() -> None:
    trusted = (
        "P^NP is not (proven to be) the same class as NP. I haven't actually discussed the paper."
    )
    candidate = "P^NP is not the same class as NP. I have actually discussed the paper."
    decision = assess_promotion(trusted, candidate, HIGH_CONFIDENCE)
    assert decision.decision == "review"
    assert "critical_token_loss" in decision.reasons
    assert "n't" in decision.missing_critical_tokens


def test_truncated_note_candidate_is_rejected() -> None:
    trusted = "NOTE: The account was blocked, but useful edits still need careful review."
    decision = assess_promotion(trusted, ":::::NOTE:", HIGH_CONFIDENCE)
    assert decision.decision == "review"
    assert "substantive_token_loss" in decision.reasons


def test_heading_and_unsigned_signature_contamination_are_rejected() -> None:
    trusted = "This is the complete substantive comment."
    candidate = (
        "This is the complete substantive comment.\n\n== Next section ==\n{{UnsignedIP|192.0.2.1}}"
    )
    decision = assess_promotion(trusted, candidate, HIGH_CONFIDENCE)
    assert decision.decision == "review"
    assert "structure:section_heading" in decision.reasons
    assert "structure:unsigned_signature_residue" in decision.reasons


def test_adjacent_utterance_contamination_is_rejected() -> None:
    trusted = "I agree that the citation should remain in the article."
    neighbor = "The next editor instead discusses a completely separate proposal with many details."
    candidate = trusted + " " + neighbor
    decision = assess_promotion(trusted, candidate, HIGH_CONFIDENCE, [neighbor])
    assert decision.decision == "review"
    assert decision.adjacent_contamination


def test_high_confidence_is_necessary_but_not_sufficient() -> None:
    same_text = "The substantive text is unchanged."
    decision = assess_promotion(
        same_text,
        same_text,
        {**HIGH_CONFIDENCE, "recovery_status": "review"},
    )
    assert decision.decision == "fallback"
    assert "v33_not_high_confidence" in decision.reasons


def test_diagnosed_terminal_unsigned_source_artifact_may_be_removed() -> None:
    trusted = "The complete comment. — Preceding unsigned comment added by 192.0.2.1"
    evidence = {
        **HIGH_CONFIDENCE,
        "source_signature_artifact_stripped": True,
        "source_signature_artifact_reason": "terminal_unsigned_attribution",
    }
    decision = assess_promotion(trusted, "The complete comment.", evidence)
    assert decision.decision == "promote"
    assert decision.trusted_comparison_adjustments == (
        "ignored_source_artifact:terminal_unsigned_attribution",
    )


def test_diagnosed_terminal_valediction_source_artifact_may_be_removed() -> None:
    trusted = "The complete comment. Best wishes"
    evidence = {
        **HIGH_CONFIDENCE,
        "source_signature_artifact_stripped": True,
        "source_signature_artifact_reason": "terminal_valediction_artifact",
    }
    decision = assess_promotion(trusted, "The complete comment.", evidence)
    assert decision.decision == "promote"


def test_unsigned_artifact_in_candidate_remains_contamination() -> None:
    trusted = "The complete comment."
    candidate = trusted + " {{UnsignedIP|192.0.2.1}}"
    decision = assess_promotion(trusted, candidate, HIGH_CONFIDENCE)
    assert decision.decision == "review"
    assert "structure:unsigned_signature_residue" in decision.reasons


@pytest.mark.parametrize(
    ("trusted", "reason"),
    [
        ("The complete comment. 66.31.39.76", "terminal_bare_ipv4_after_sentence"),
        (
            "The complete comment. — Preceding unsigned comment added by 192.0.2.1",
            "terminal_unsigned_attribution_v32",
        ),
        ("The complete comment. – ·", "terminal_wikidisputes_signature_glyphs"),
        ("The complete comment. -- 192.0.2.1", "terminal_explicit_ip_signature"),
    ],
)
def test_certified_current_source_artifacts_are_matching_only(trusted: str, reason: str) -> None:
    frozen_trusted = trusted
    evidence = {
        **HIGH_CONFIDENCE,
        "source_signature_artifact_stripped": True,
        "source_signature_artifact_reason": reason,
    }

    decision = assess_promotion(trusted, "The complete comment.", evidence)

    assert trusted == frozen_trusted
    assert decision.decision == "promote"
    assert decision.trusted_comparison_adjustments == (f"ignored_source_artifact:{reason}",)


def test_certified_artifact_reason_without_fallback_does_not_clean_source() -> None:
    trusted = "The complete comment. 66.31.39.76"
    decision = assess_promotion(
        trusted,
        "The complete comment.",
        {
            **HIGH_CONFIDENCE,
            "source_signature_artifact_stripped": False,
            "source_signature_artifact_reason": "terminal_bare_ipv4_after_sentence",
        },
    )

    assert decision.decision == "review"
    assert decision.trusted_comparison_adjustments == ()
