from __future__ import annotations

import json

import pytest

from wikidisputes_ssot.revision_diff.llm_audit_windows import (
    build_raw_windows,
    format_review_object,
    validate_interval_text,
)

RAW = (
    "== Topic ==\n"
    "First comment. -- [[User:Alice]] 12:00, 1 January 2020 (UTC)\n"
    ":Second focal comment. -- [[User:Bob]] 12:01, 1 January 2020 (UTC)\n"
    "::Third comment. -- [[User:Carol]] 12:02, 1 January 2020 (UTC)\n"
)


def test_windows_preserve_focal_range_and_absolute_slice() -> None:
    start = RAW.index("Second focal")
    end = start + len("Second focal comment. -- [[User:Bob]] 12:01, 1 January 2020 (UTC)")
    windows = build_raw_windows(RAW, [(start, end)], context_characters=0)
    assert len(windows) == 1
    assert windows[0]["start"] <= start < end <= windows[0]["end"]
    assert windows[0]["raw_text"] == RAW[windows[0]["start"] : windows[0]["end"]]
    # Adjacent signed structural comments are present, not a character crop.
    assert "First comment." in windows[0]["raw_text"]
    assert "Third comment." in windows[0]["raw_text"]


def test_distant_focal_ranges_produce_disjoint_exact_windows() -> None:
    raw = "one\n" + ("filler\n" * 100) + "two\n"
    windows = build_raw_windows(raw, [(0, 3), (len(raw) - 4, len(raw) - 1)], context_characters=0)
    assert len(windows) == 2
    assert windows[0]["end"] <= windows[1]["start"]
    assert all(item["raw_text"] == raw[item["start"] : item["end"]] for item in windows)


def test_invalid_interval_or_candidate_text_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not match"):
        validate_interval_text("abcdef", 1, 4, "wrong")
    with pytest.raises(ValueError, match="invalid raw interval"):
        validate_interval_text("abcdef", 4, 7, "efx")


def test_review_object_is_blinded_and_keeps_candidate_and_sample_evidence() -> None:
    start = RAW.index("Second focal")
    end = start + len("Second focal comment. -- [[User:Bob]] 12:01, 1 January 2020 (UTC)")
    result = format_review_object(
        {
            "audit_uid": "audit-1",
            "source_row_uid": "source-1",
            "target_revision_id": "12",
            "source_text": "Second focal comment.",
            "target_wikitext": RAW,
            "candidate_start": start,
            "candidate_end": end,
            "candidate_raw": RAW[start:end],
            "all_candidates": [
                {"start": start, "end": end, "raw_wikitext": RAW[start:end]},
                {
                    "start": RAW.index("Third comment."),
                    "end": RAW.index("Third comment.")
                    + len("Third comment. -- [[User:Carol]] 12:02, 1 January 2020 (UTC)"),
                    "raw_wikitext": "Third comment. -- [[User:Carol]] 12:02, 1 January 2020 (UTC)",
                },
            ],
            "target_changed_ranges_json": json.dumps([[start, end]]),
            "competing_candidates_json": json.dumps(["comment:0"]),
            "primary_stratum": "b_review",
            "inclusion_probability": 0.2,
            "survey_weight": 5.0,
            "assignment_evidence_json": '["overlap"]',
            "method_b_status": "b_review",
            "candidate_score": 0.8,
        }
    )
    assert result["candidates"][0] == {"start": start, "end": end, "raw_text": RAW[start:end]}
    assert any(
        candidate["raw_text"].startswith("Third comment.") for candidate in result["candidates"]
    )
    assert result["target_windows"]
    assert result["neighboring_structural_units"]
    assert result["sample_design"]["survey_weight"] == 5.0
    assert "method_b_status" not in result["evidence"]
    assert "candidate_score" not in result["evidence"]
    assert "status" not in result["review_context"]
    encoded = json.dumps(result, ensure_ascii=False)
    assert "Second focal comment." in encoded
