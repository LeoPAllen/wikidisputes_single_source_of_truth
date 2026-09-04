from wikidisputes_ssot.revision_diff.pilot_comparison import (
    SOFT_USABILITY_REASONS,
    boundary_usable_fix_comparison_report,
    comparison_metrics,
    normalize_offset,
)


def _row(uid: str, status: str, *, reasons: str = "[]") -> dict[str, object]:
    return {
        "source_row_uid": uid,
        "method_a_status": "fallback",
        "method_b_status": status,
        "assignment_status": "assigned",
        "reason_codes_json": reasons,
        "lifecycle": "creation",
        "candidate": {"comparable": True, "raw_equal": True, "visible_equal": True},
        "left_boundaries": {"method_a": "123", "method_b": 123},
        "right_boundaries": {"method_a": "124", "method_b": 124},
    }


def test_offset_normalization_is_comparison_only() -> None:
    assert normalize_offset("123") == 123
    assert normalize_offset(123) == 123
    assert normalize_offset(True) is None
    assert normalize_offset("12.3") is None
    assert normalize_offset("") is None


def test_comparison_metrics_normalize_boundaries_and_exclude_malformed() -> None:
    rows = [
        _row("one", "b_safe"),
        {
            **_row("two", "b_safe"),
            "left_boundaries": {"method_a": "123", "method_b": 124},
            "right_boundaries": {"method_a": "bad", "method_b": 124},
        },
    ]
    metrics = comparison_metrics(rows)
    assert metrics["left_boundary"] == {
        "comparable_count": 2,
        "agreement_count": 1,
        "agreement_rate": 0.5,
    }
    assert metrics["right_boundary"] == {
        "comparable_count": 1,
        "agreement_count": 1,
        "agreement_rate": 1.0,
    }
    assert metrics["both_boundaries"]["comparable_count"] == 1


def test_comparison_metrics_preserve_zero_offsets() -> None:
    row = _row("zero", "b_safe")
    row["left_boundaries"] = {"method_a": 0, "method_b": "0"}
    metrics = comparison_metrics([row])
    assert metrics["left_boundary"]["agreement_count"] == 1


def test_boundary_usable_comparison_accounts_for_statuses_soft_reasons_and_cases() -> None:
    suffix = "0c78c7423168a1a3c159"
    before = [_row(f"wdrow:v1:{suffix}", "b_review")]
    after = [
        {
            **_row(f"wdrow:v1:{suffix}", "b_usable", reasons='["structure:terminal_signature"]'),
            "candidate_start": 12,
            "candidate_end": 34,
        }
    ]
    report = boundary_usable_fix_comparison_report(before, after, expected_rows=1)

    assert report["same_source_row_uid_set"] == "PASS"
    assert report["assignment_limits_changed"] is False
    assert report["assignment_limits"] == {
        "actions": 10,
        "candidates": 30,
        "edges": 200,
        "states": 100_000,
    }
    assert report["b_safe_semantics_changed"] is False
    assert report["after"]["method_b_status_counts"]["b_usable"] == 1
    assert report["after"]["b_usable_reason_distribution"] == {"structure:terminal_signature": 1}
    assert report["after"]["b_usable_reasons_all_soft"] is True
    assert report["after"]["fallback_to_safe_usable"] == {
        "count": 1,
        "to_b_safe": 0,
        "to_b_usable": 1,
    }
    assert report["tracked_boundary_cases"]["after"][suffix] == {
        "mapping": "matched",
        "source_row_uid": f"wdrow:v1:{suffix}",
        "candidate_start": 12,
        "candidate_end": 34,
        "method_b_status": "b_usable",
    }
    assert SOFT_USABILITY_REASONS


def test_comparison_flags_any_non_soft_usable_reason() -> None:
    row = _row("one", "b_usable", reasons='["lifecycle:inconsistent"]')
    report = boundary_usable_fix_comparison_report([row], [row], expected_rows=1)
    assert report["after"]["b_usable_reasons_all_soft"] is False
    assert report["after"]["b_usable_non_soft_reasons"] == ["lifecycle:inconsistent"]


def test_tracked_cases_resolve_audit_uid_suffixes_through_key() -> None:
    suffix = "0c78c7423168a1a3c159"
    entity_uid = "wdrow:v1:source"
    row = _row(entity_uid, "b_safe")
    report = boundary_usable_fix_comparison_report(
        [row],
        [row],
        expected_rows=1,
        tracked_uid_suffixes=(suffix,),
        audit_uid_to_entity_uid={f"revision-diff-audit:{suffix}": entity_uid},
    )
    tracked = report["tracked_boundary_cases"]["after"][suffix]
    assert tracked["mapping"] == "matched"
    assert tracked["audit_uid"] == f"revision-diff-audit:{suffix}"
    assert tracked["source_row_uid"] == entity_uid
