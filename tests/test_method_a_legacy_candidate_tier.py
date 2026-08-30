from __future__ import annotations

import importlib.util
from pathlib import Path

from wikidisputes_ssot.legacy_timestamp_regions import FROZEN_SOURCE_REVISION
from wikidisputes_ssot.promotion_safety import assess_promotion

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "recover_raw_mediawiki_comments.py"
SPEC = importlib.util.spec_from_file_location("method_a_legacy_tier", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)


def _signed(body: str) -> str:
    return f"{body} --[[User:Alice|Alice]] 12:34, 8 July 2005 (UTC)"


def test_current_high_confidence_candidate_has_no_replacing_legacy_hypothesis() -> None:
    raw = _signed("Canonical complete comment.")
    current = RECOVERY.candidate_comments(raw)
    ranked = RECOVERY.rank_candidates("Canonical complete comment.", current, action_offset=0)

    status, _ = RECOVERY.classify(ranked[0], None)

    assert status == "high_confidence"
    # The legacy extractor's only range is the canonical range, so the
    # additional-hypothesis tier has nothing to replace it with.
    assert RECOVERY.legacy_candidate_comments(raw, current) == []


def test_legacy_high_confidence_label_cannot_bypass_promotion_safety() -> None:
    raw = "Complete comment.\n== Later topic ==\n" + _signed("A later signed comment.")
    current = RECOVERY.candidate_comments(raw)
    legacy = RECOVERY.legacy_candidate_comments(raw, current)

    assert len(legacy) == 1
    legacy_body = legacy[0]["body_without_signature"]
    ranked = RECOVERY.rank_candidates(legacy_body, legacy, action_offset=0)
    status, margin = RECOVERY.classify(ranked[0], None)

    assert status == "high_confidence"
    decision = assess_promotion(
        legacy_body,
        legacy_body,
        {
            "recovery_status": status,
            "target_coverage": ranked[0]["target_coverage"],
            "candidate_purity": ranked[0]["candidate_purity"],
            "match_margin": margin,
            "signature_residue_detected": ranked[0]["signature_residue_detected"],
        },
    )

    assert decision.decision == "review"
    assert "structure:section_heading" in decision.reasons


def test_legacy_hypothesis_has_distinct_range_and_frozen_provenance() -> None:
    raw = "Earlier context.\n== Later topic ==\n" + _signed("Signed comment.")
    current = RECOVERY.candidate_comments(raw)
    legacy = RECOVERY.legacy_candidate_comments(raw, current)

    assert len(legacy) == 1
    assert (legacy[0]["start"], legacy[0]["end"]) not in {
        (candidate["start"], candidate["end"]) for candidate in current
    }
    assert legacy[0]["tier"] == "legacy_candidate_current_safety"
    assert legacy[0]["provenance"] == ("legacy_candidate_current_safety:" + FROZEN_SOURCE_REVISION)
