from __future__ import annotations

import pytest

from wikidisputes_ssot.method_a_comparison import HardRegressionError, compare_method_a


def _recovery(uid: str, **extra: object) -> dict[str, object]:
    return {
        "source_row_uid": uid,
        "raw_start": 1,
        "raw_end": 9,
        "recovered_raw_wikitext": "raw",
        "recovered_body_wikitext": "body",
        "recovery_status": "high_confidence",
        **extra,
    }


def _audit(uid: str, decision: str) -> dict[str, str]:
    return {"source_row_uid": uid, "decision": decision}


def test_report_counts_unique_gains_tiers_and_method_b_overlap() -> None:
    old_recovery = [
        _recovery("old"),
        _recovery("new-h", boundary_method="historical_signature_fallback"),
        _recovery("new-c", source_signature_artifact_stripped=True),
        _recovery("new-l", tier="legacy_candidate_current_safety"),
    ]
    old_audit = [
        _audit("old", "promote"),
        _audit("new-h", "fallback"),
        _audit("new-c", "review"),
        _audit("new-l", "fallback"),
    ]
    new_audit = [
        _audit("old", "promote"),
        _audit("new-h", "promote"),
        _audit("new-c", "promote"),
        _audit("new-l", "promote"),
    ]
    report = compare_method_a(
        old_recovery,
        old_audit,
        old_recovery,
        new_audit,
        [
            {"source_row_uid": "new-h", "status": "b_safe"},
            {"source_row_uid": "new-c", "status": "b_usable"},
            {"source_row_uid": "outside", "status": "b_safe"},
        ],
        denominator=10,
    )
    assert report["method_a_decisions"] == {
        "before": {"promote": 1, "review": 1, "fallback": 2},
        "after": {"promote": 4, "review": 0, "fallback": 0},
    }
    assert report["new_promotions"]["by_recovery_tier"] == {
        "historical_signature_fallback": 1,
        "certified_source_artifact": 1,
        "historical_signature_certified_source_artifact": 0,
        "legacy_candidate_current_safety": 1,
    }
    assert report["method_a_method_b"] == {
        "method_b_safe_or_b_usable": 2,
        "new_a_promotions_overlapping_b_safe_or_b_usable": 2,
        "new_unique_a_gains": 1,
        "total_unique_validated_a_plus_b": 4,
        "total_unique_validated_a_plus_b_percentage": 40.0,
    }


def test_hard_regressions_preserve_exact_values_and_raise() -> None:
    before = [_recovery("same")]
    after = [
        _recovery(
            "same",
            raw_start=2,
            recovered_raw_wikitext="raw changed",
            recovered_body_wikitext="body changed",
            recovery_status="review",
        )
    ]
    with pytest.raises(HardRegressionError):
        compare_method_a(
            before, [_audit("same", "promote")], after, [_audit("same", "promote")], []
        )
    report = compare_method_a(
        before,
        [_audit("same", "promote")],
        after,
        [_audit("same", "promote")],
        [],
        raise_on_regression=False,
    )
    assert report["regressions"]["hard_regression_uid_count"] == 1
    assert report["regressions"]["raw_interval_changes"][0]["before"] == (1, 9)
    assert report["regressions"]["raw_interval_changes"][0]["after"] == (2, 9)
    assert report["regressions"]["raw_text_changes"][0]["before"] == "raw"
    assert report["regressions"]["raw_text_changes"][0]["after"] == "raw changed"
    assert report["regressions"]["body_text_changes"][0]["before"] == "body"
    assert report["regressions"]["body_text_changes"][0]["after"] == "body changed"
    assert report["regressions"]["status_changes"][0]["before"] == "high_confidence"
    assert report["regressions"]["status_changes"][0]["after"] == "review"
    assert report["regressions"]["status_changes"][0]["before_sha256"]


def test_lost_baseline_promotion_is_a_hard_regression() -> None:
    before = [_recovery("same")]
    after = [_recovery("same")]
    before_audit = [_audit("same", "promote")]
    after_audit = [_audit("same", "fallback")]

    with pytest.raises(HardRegressionError):
        compare_method_a(before, before_audit, after, after_audit, [])

    report = compare_method_a(
        before,
        before_audit,
        after,
        after_audit,
        [],
        raise_on_regression=False,
    )

    assert report["regressions"]["lost_promotions"] == [{"source_row_uid": "same"}]
    assert report["regressions"]["hard_regression_uid_count"] == 1


def test_uid_set_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="source_row_uid sets differ"):
        compare_method_a(
            [_recovery("one")],
            [_audit("one", "promote")],
            [_recovery("two")],
            [_audit("two", "promote")],
            [],
        )
