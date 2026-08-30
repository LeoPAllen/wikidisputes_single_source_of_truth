from __future__ import annotations

import pytest

from wikidisputes_ssot.method_a_additive import build_additive_rows


def _candidate(
    *, tier: str | None = None, artifact: bool = False, legacy_label: str = "old_hc"
) -> dict:
    value = {
        "similarity": 1.0,
        "start": 4,
        "end": 9,
        "raw": "raw",
        "body_without_signature": "body",
        "boundary_method": "boundary",
        "target_coverage": 1.0,
        "candidate_purity": 1.0,
        "normalized_length_delta": 0,
        "signature_residue_detected": False,
        "hc_safety_reason": "",
        "source_comparison_mode": "certified_source_artifact" if artifact else "exact",
        "legacy_label": legacy_label,
    }
    if tier:
        value["tier"] = tier
    return value


def _run(rows, audits, candidates, legacy, classify=lambda best, second: ("high_confidence", 1.0)):
    return build_additive_rows(
        rows,
        audits,
        lambda rid: ("revision", "User") if rid == 1 else None,
        lambda raw, user: candidates,
        lambda target, values, offset: values,
        classify,
        lambda raw, values, user: legacy,
        legacy_source_revision="frozen",
        expected_uid_count=len(rows),
        expected_promote_count=sum(row["decision"] == "promote" for row in audits),
    )


def test_promoted_baseline_is_copied_and_order_is_preserved() -> None:
    rows = [
        {
            "source_row_uid": "a",
            "revision_id": "1",
            "action_offset": "1",
            "source_text": "x",
            "raw_start": "old",
            "custom": "keep",
        },
        {"source_row_uid": "b", "revision_id": "1", "action_offset": "2", "source_text": "x"},
    ]
    output, _ = _run(
        rows,
        [
            {"source_row_uid": "a", "decision": "promote"},
            {"source_row_uid": "b", "decision": "fallback"},
        ],
        [_candidate(tier="historical_signature_fallback")],
        [],
    )
    assert output[0] == rows[0]
    assert [row["source_row_uid"] for row in output] == ["a", "b"]


def test_promoted_baseline_is_copied_without_any_recovery_callbacks() -> None:
    rows = [
        {
            "source_row_uid": "a",
            "revision_id": "1",
            "action_offset": "1",
            "source_text": "x",
            "recovered_raw_wikitext": "immutable raw",
            "recovered_body_wikitext": "immutable body",
            "custom": {"nested": ["value"]},
        }
    ]

    def fail(*_args: object) -> object:
        raise AssertionError("baseline-promoted rows must not invoke recovery callbacks")

    output, counts = build_additive_rows(
        rows,
        [{"source_row_uid": "a", "decision": "promote"}],
        fail,
        fail,
        fail,
        fail,
        fail,
        legacy_source_revision="frozen",
        expected_uid_count=1,
        expected_promote_count=1,
    )

    assert output == rows
    assert counts["baseline_promote_preserved"] == 1


@pytest.mark.parametrize(
    ("revision_id", "action_offset", "description"),
    [
        ("2", "1", "unavailable revision"),
        ("not-an-id", "1", "invalid revision id"),
        ("1", "not-an-offset", "invalid action offset"),
        ("1", "1", "no candidates"),
    ],
)
def test_unavailable_invalid_and_no_candidate_rows_are_retained(
    revision_id: str, action_offset: str, description: str
) -> None:
    rows = [
        {
            "source_row_uid": "a",
            "revision_id": revision_id,
            "action_offset": action_offset,
            "source_text": "x",
            "description": description,
        }
    ]
    output, counts = _run(
        rows,
        [{"source_row_uid": "a", "decision": "fallback"}],
        [],
        [],
        lambda best, second: ("review", None),
    )

    assert output == rows
    assert counts["baseline_retained"] == 1


def test_fallback_tier_priority_prefers_historical_then_artifact() -> None:
    rows = [{"source_row_uid": "a", "revision_id": "1", "action_offset": "1", "source_text": "x"}]
    audit = [{"source_row_uid": "a", "decision": "fallback"}]
    output, _ = _run(
        rows,
        audit,
        [_candidate(tier="historical_signature_fallback", artifact=True)],
        [_candidate()],
    )
    assert output[0]["recovery_tier"] == "historical_signature_certified_source_artifact"


def test_legacy_label_does_not_promote_without_current_classification() -> None:
    rows = [{"source_row_uid": "a", "revision_id": "1", "action_offset": "1", "source_text": "x"}]
    audit = [{"source_row_uid": "a", "decision": "fallback"}]
    output, counts = _run(
        rows,
        audit,
        [],
        [_candidate(legacy_label="historically_high_confidence")],
        lambda best, second: ("review", 0.5),
    )
    assert output[0] == rows[0]
    assert counts.get("legacy_candidate_current_safety", 0) == 0


def test_uid_set_must_match() -> None:
    rows = [{"source_row_uid": "a", "revision_id": "1", "action_offset": "1", "source_text": "x"}]
    try:
        _run(rows, [{"source_row_uid": "other", "decision": "fallback"}], [], [])
    except ValueError as exc:
        assert "UID sets differ" in str(exc)
    else:
        raise AssertionError("expected UID validation failure")


def test_frozen_promote_control_count_must_match() -> None:
    rows = [{"source_row_uid": "a", "revision_id": "1", "action_offset": "1"}]
    with pytest.raises(ValueError, match="baseline promote count=1; expected 85,185"):
        build_additive_rows(
            rows,
            [{"source_row_uid": "a", "decision": "promote"}],
            lambda _rid: None,
            lambda _raw, _user: [],
            lambda _target, _values, _offset: [],
            lambda _best, _second: ("unresolved_no_candidate", None),
            lambda _raw, _values, _user: [],
            legacy_source_revision="frozen",
            expected_uid_count=1,
        )
