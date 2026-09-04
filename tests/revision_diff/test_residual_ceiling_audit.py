from __future__ import annotations

import csv

import pytest

from wikidisputes_ssot.revision_diff.residual_ceiling_audit import (
    build_residual_audit_packet,
    label_audit_row,
)


def _rows() -> list[dict[str, object]]:
    return [
        {
            "audit_uid": "row-1",
            "source_text": "source comment",
            "target_wikitext": "before target change after",
            "target_changed_start": 7,
            "target_changed_end": 20,
            "predecessor_wikitext": "old target",
            "candidate_body": "target change",
            "candidate_start": 7,
            "candidate_end": 20,
            "target_changed_ranges_json": "[[7, 20]]",
            "signature_author": "Example",
            "method_b_status": "b_review",
            "candidate_score": 0.2,
            "discussiontools_evidence": True,
            "discussiontools_html": "<p>evidence</p>",
            "survey_weight": 12.5,
        }
    ]


def test_packet_has_evidence_and_blinds_status_and_scores_in_html(tmp_path) -> None:
    csv_path = tmp_path / "packet.csv"
    html_path = tmp_path / "packet.html"
    result = build_residual_audit_packet(
        _rows(), csv_path=csv_path, html_path=html_path, metadata={"seed": 20260831}
    )
    assert result["rows"] == 1
    with csv_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["target_raw_revision_excerpt"]
    assert len(row["target_raw_revision_excerpt"]) <= 901
    assert row["discussiontools_evidence"]
    assert row["recoverability"] == ""
    assert "target_wikitext" not in row
    assert "predecessor_wikitext" not in row
    assert "diff_operations_json" not in row
    rendered = html_path.read_text(encoding="utf-8")
    assert "target change" in rendered
    assert "b_review" not in rendered
    assert "0.2" not in rendered


def test_label_persists_immediately_and_rerenders(tmp_path) -> None:
    csv_path = tmp_path / "packet.csv"
    html_path = tmp_path / "packet.html"
    build_residual_audit_packet(_rows(), csv_path=csv_path, html_path=html_path)
    saved = label_audit_row(
        csv_path,
        "row-1",
        recoverability="deterministic_rule_possible",
        chosen_candidate="existing_candidate",
        manual_raw_start=7,
        manual_raw_end=20,
        candidate_error="exact",
        rule_family="diff_span",
        confidence="high",
        evidence_note="contiguous target span",
        html_path=html_path,
    )
    assert saved["recoverability"] == "deterministic_rule_possible"
    with csv_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["manual_raw_start"] == "7"
    assert "deterministic_rule_possible" in html_path.read_text(encoding="utf-8")


def test_label_validation_and_safe_no_overwrite(tmp_path) -> None:
    csv_path = tmp_path / "packet.csv"
    html_path = tmp_path / "packet.html"
    build_residual_audit_packet(_rows(), csv_path=csv_path, html_path=html_path)
    with pytest.raises(ValueError, match="invalid recoverability"):
        label_audit_row(csv_path, "row-1", recoverability="guess")
    with pytest.raises(ValueError, match="supplied together"):
        label_audit_row(
            csv_path,
            "row-1",
            recoverability="human_only",
            manual_raw_start=7,
            confidence="medium",
            evidence_note="manual boundary",
        )
    with pytest.raises(ValueError, match="0 <= start < end"):
        label_audit_row(
            csv_path,
            "row-1",
            recoverability="human_only",
            manual_raw_start=20,
            manual_raw_end=7,
            confidence="medium",
            evidence_note="manual boundary",
        )
    label_audit_row(
        csv_path,
        "row-1",
        recoverability="human_only",
        manual_raw_start=7,
        manual_raw_end=20,
        confidence="medium",
        evidence_note="manual boundary",
    )
    with pytest.raises(ValueError, match="refusing to overwrite"):
        build_residual_audit_packet(_rows(), csv_path=csv_path, html_path=html_path)


def test_packet_omits_absent_discussiontools_evidence(tmp_path) -> None:
    source = _rows()[0]
    source["discussiontools_evidence"] = False
    source.pop("discussiontools_html")
    csv_path = tmp_path / "packet.csv"
    html_path = tmp_path / "packet.html"
    build_residual_audit_packet([source], csv_path=csv_path, html_path=html_path)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["discussiontools_evidence"] == ""
