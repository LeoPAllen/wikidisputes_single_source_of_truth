from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "compare_method_b_evidence.py"
SPEC = importlib.util.spec_from_file_location("method_b_evidence_comparison", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _row(uid: str, status: str = "b_safe", **extra: object) -> dict[str, object]:
    return {
        "source_row_uid": uid,
        "logical_utterance_uid": f"logical:{uid}",
        "action_uid": f"action:{uid}",
        "action_type": "creation",
        "target_revision_id": 101,
        "predecessor_revision_id": 100,
        "page_id": 7,
        "method": "mediawiki_revision_diff",
        "method_version": "1.0.0",
        "safety_version": "method-b-safety-v2",
        "schema_version": 1,
        "target_api_sha1": "target-sha1",
        "predecessor_api_sha1": "predecessor-sha1",
        "target_local_content_sha256": "target-local",
        "predecessor_local_content_sha256": "predecessor-local",
        "target_response_hash": "target-response",
        "predecessor_response_hash": "predecessor-response",
        "target_content_pointer": "target-pointer",
        "predecessor_content_pointer": "predecessor-pointer",
        "status": status,
        "candidate_raw": "raw",
        "candidate_body": "body",
        **extra,
    }


def test_unchanged_selectable_controls_pass() -> None:
    report = MODULE.compare_method_b_evidence(
        [_row("safe"), _row("usable", "b_usable"), _row("review", "b_review")],
        [_row("safe"), _row("usable", "b_usable"), _row("review", "b_no_candidate")],
    )
    assert report["status"] == "pass"
    assert report["regressions"]["hard_regression_count"] == 0


def test_missing_status_candidate_and_provenance_are_reported_and_raise() -> None:
    baseline = [_row("lost"), _row("changed")]
    current = [
        _row(
            "changed",
            "b_review",
            candidate_raw="new raw",
            candidate_body="new body",
            target_response_hash="changed-response",
        )
    ]
    with pytest.raises(MODULE.MethodBEvidenceRegressionError):
        MODULE.compare_method_b_evidence(baseline, current)
    report = MODULE.compare_method_b_evidence(baseline, current, raise_on_regression=False)
    assert report["regressions"]["lost_rows"] == [{"source_row_uid": "lost"}]
    assert len(report["regressions"]["status_changes"]) == 1
    assert len(report["regressions"]["candidate_raw_changes"]) == 1
    assert len(report["regressions"]["candidate_body_changes"]) == 1
    assert report["regressions"]["provenance_mismatches"][0]["field"] == "target_response_hash"


def test_nonselectable_baseline_rows_are_not_controls() -> None:
    baseline = [_row("review", "b_review")]
    current = [_row("review", "b_safe", candidate_raw="different")]
    report = MODULE.compare_method_b_evidence(baseline, current)
    assert report["baseline_selectable_rows"] == 0
    assert report["regressions"]["hard_regression_count"] == 0


def test_duplicate_uid_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate source_row_uid"):
        MODULE.compare_method_b_evidence([_row("same"), _row("same")], [_row("same")])
