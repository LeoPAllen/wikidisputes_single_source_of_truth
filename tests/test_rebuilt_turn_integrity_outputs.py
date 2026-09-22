"""Regression checks on the materialized annotation and Gold deliverables."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import duckdb
import pytest
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
ANNOTATION = ROOT / "output/annotation/wikidisputes_llm_annotation_input.csv"
DECISIONS = ROOT / "output/silver/turn_integrity_decisions.parquet"
GOLD = ROOT / "output/annotation/gold_input_ssot_annotation_ready.xlsx"
MANIFEST = ROOT / "output/annotation/annotation_manifest.json"
SUMMARY = ROOT / "output/reports/turn_integrity/repair_summary.json"


@pytest.fixture(scope="module")
def rebuilt_outputs() -> tuple[
    list[dict[str, str]], list[dict[str, object]], list[dict[str, object]]
]:
    if not all(path.exists() for path in (ANNOTATION, DECISIONS, GOLD)):
        pytest.skip("materialized annotation, decisions, and Gold are required")
    with ANNOTATION.open(newline="", encoding="utf-8") as handle:
        annotation_rows = list(csv.DictReader(handle))
    connection = duckdb.connect()
    try:
        cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(DECISIONS)])
        columns = [item[0] for item in cursor.description]
        decisions = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        connection.close()
    workbook = load_workbook(GOLD, read_only=True, data_only=True)
    try:
        sheet = workbook["Gold_Annotation"]
        records = sheet.values
        headers = next(records)
        gold_rows = [dict(zip(headers, row, strict=True)) for row in records]
    finally:
        workbook.close()
    return annotation_rows, decisions, gold_rows


def test_named_repairs_reach_final_annotation_and_decisions(rebuilt_outputs) -> None:
    annotation_rows, decisions, _ = rebuilt_outputs

    def exported(dispute: str) -> list[dict[str, str]]:
        return [row for row in annotation_rows if row["dispute_sequence"] == dispute]

    def cases(dispute: str) -> list[dict[str, object]]:
        return [row for row in decisions if row["dispute_id"] == dispute]

    d88 = exported("D08854")
    long_representations = [
        row
        for row in d88
        if row["utterance_text"].startswith("Someone's been trying to insert clearly fictitious")
    ]
    assert len(long_representations) == 3
    assert len({row["utterance_id"] for row in long_representations}) == 3
    assert all(
        row["ssot_turn_integrity_decision_reason"] == "unresolved_replay_identity"
        for row in long_representations
    )
    d88_aliases = [
        row for row in cases("D08854") if row["final_disposition"] == "alias_or_suppress_duplicate"
    ]
    assert d88_aliases
    assert not {str(row["utterance_id"]) for row in d88_aliases} & {
        row["utterance_id"] for row in d88
    }

    d69 = exported("D06910")
    repeated_ids = {"65956163.2174.2174", "65956163.8647.7947"}
    assert {row["utterance_id"] for row in d69} >= repeated_ids
    assert len({row["utterance_text"] for row in d69 if row["utterance_id"] in repeated_ids}) == 1
    assert {
        str(row["utterance_id"])
        for row in cases("D06910")
        if row["problem_type"] == "exact_replay" and row["final_disposition"] == "keep"
    } >= repeated_ids

    for dispute in ("D01054", "D02194"):
        assert all(
            row["ssot_turn_integrity_disposition"] != "alias_or_suppress_duplicate"
            for row in exported(dispute)
        )
        assert not any(
            row["final_disposition"] == "alias_or_suppress_duplicate" for row in cases(dispute)
        )

    split = [row for row in exported("D01057") if row["ssot_turn_integrity_disposition"] == "split"]
    assert [row["speaker_id"] for row in split] == [
        "Still-24-45-42-125",
        "Belchfire",
        "Still-24-45-42-125",
    ]
    assert len({row["utterance_id"] for row in split}) == 3
    assert (
        len(
            [
                row
                for row in exported("D07250")
                if row["ssot_turn_integrity_disposition"] == "reattached_fragment"
            ]
        )
        == 1
    )
    for dispute in ("D01703", "D06411"):
        assert any(str(row["decision_reason"]).startswith("unresolved_") for row in cases(dispute))
        assert all(
            row["ssot_turn_integrity_disposition"] not in {"split", "reattached_fragment"}
            for row in exported(dispute)
        )
        assert all(row["utterance_text"].strip() for row in exported(dispute))


def test_gold_membership_and_order_follow_export_without_claiming_readiness(
    rebuilt_outputs,
) -> None:
    annotation_rows, _, gold_rows = rebuilt_outputs
    gold_by_dispute: dict[str, list[dict[str, object]]] = {}
    for row in gold_rows:
        gold_by_dispute.setdefault(str(row["dispute_id"]), []).append(row)
    for dispute_id, rows in gold_by_dispute.items():
        current = [row for row in annotation_rows if row["dispute_id"] == dispute_id]
        assert Counter(str(row["utterance_id"]) for row in rows) == Counter(
            row["utterance_id"] for row in current
        )
        assert Counter(
            (str(row["utterance_id"]), str(row["speaker_id"]), str(row["utterance_text"]))
            for row in rows
        ) == Counter(
            (row["utterance_id"], row["speaker_id"], row["utterance_text"]) for row in current
        )
        orders = [int(row["utterance_order"]) for row in rows if row["utterance_order"] is not None]
        assert orders == sorted(orders)
    assert any(row["provenance"] == "needs_rereview" for row in gold_rows)
    for dispute in ("D01342", "D03503", "D01493", "D05465"):
        assert any(row["dispute_sequence"] == dispute for row in gold_rows)
    assert any(row["utterance_text"] == ":It is about you." for row in gold_rows)
    assert any(row["utterance_text"] == ":agree." for row in gold_rows)
    assert any(row["utterance_text"] == "::Yes!" for row in gold_rows)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    assert summary["qc"]["missing_candidate_identities"] == []
    assert summary["qc"]["candidate_without_case_count"] == 0
    readiness = manifest["gold_annotation_readiness"]
    assert readiness["annotation_ready"] is False
    assert readiness["blocking_case_count"] == len(readiness["blocking_cases"])
    assert all(
        case["type"] != "changed_unit" for case in readiness["blocking_cases"] if case["case_id"]
    )
    assert manifest["annotation_ready"] is False


def test_flagged_gold_sample_is_retained_and_explicitly_marked_for_review(
    rebuilt_outputs,
) -> None:
    annotation_rows, _, gold_rows = rebuilt_outputs
    flagged = {
        "181458312.3149.3149": "unresolved_high_confidence_merged_comment",
        "456480188.21216.21216": "unresolved_high_confidence_merged_comment",
        "32922858.1612.1612": "unresolved_high_confidence_merged_comment",
        "turn-unit:v1:40b0ca5c6c0ef458fa845027098ae462b80dbc4a465d025bcea6b5f072120015": (
            "split_child_requires_annotation"
        ),
        "turn-unit:v1:e3e389348d89a60e702d38a0a167122009877c125e81962f93d1c0bebb78de92": (
            "split_child_requires_annotation"
        ),
        "turn-unit:v1:0282925f61ce87c7ba5e85e22d24087b09d5f507d4b7e237efef98d5db042334": (
            "split_child_requires_annotation"
        ),
        "260624906.75956.75956": "unresolved_high_confidence_merged_comment",
        "262033534.90126.90126": "unresolved_speaker_signature_conflict",
        "308287895.17875.17875": "unresolved_fragment_evidence",
        "714860846.99114.99114": "unresolved_speaker_signature_conflict",
        "715133338.29482.29482": "unresolved_high_confidence_merged_comment",
        "715154315.29840.29840": "unresolved_fragment_evidence",
        "106689031.131779.131779": "unresolved_high_confidence_merged_comment",
        "19984399.48214.47641": "unresolved_fragment_evidence",
        "133430871.30166.30166": "unresolved_high_confidence_merged_comment",
        "133801892.36236.36236": "unresolved_high_confidence_merged_comment",
        "133975276.36783.36783": "unresolved_high_confidence_merged_comment",
        "151460730.54141.54141": "unresolved_high_confidence_merged_comment",
        "662046446.113863.113863": "unresolved_high_confidence_merged_comment",
    }
    assert len(flagged) == 19
    annotation_by_id = {row["utterance_id"]: row for row in annotation_rows}
    gold_by_id = {str(row["utterance_id"]): row for row in gold_rows}
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    blockers_by_id = {
        row["utterance_id"]: row for row in manifest["gold_annotation_readiness"]["blocking_cases"]
    }
    for utterance_id, reason in flagged.items():
        assert annotation_by_id[utterance_id]["needs_rereview"] == "true"
        assert gold_by_id[utterance_id]["provenance"] == "needs_rereview"
        assert blockers_by_id[utterance_id]["type"] == reason
        assert (
            gold_by_id[utterance_id]["utterance_text"]
            == annotation_by_id[utterance_id]["utterance_text"]
        )
        assert (
            gold_by_id[utterance_id]["speaker_id"] == annotation_by_id[utterance_id]["speaker_id"]
        )
    split_ids = [
        utterance_id for utterance_id, reason in flagged.items() if reason.startswith("split_")
    ]
    assert [row["speaker_id"] for row in gold_rows if str(row["utterance_id"]) in split_ids] == [
        "Still-24-45-42-125",
        "Belchfire",
        "Still-24-45-42-125",
    ]
    short_reply = gold_by_id["19984399.48214.47641"]
    assert short_reply["utterance_text"] == "::Yes!"
