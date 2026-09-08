from __future__ import annotations

import csv
import hashlib
from collections import Counter
from itertools import pairwise
from pathlib import Path

import duckdb
from openpyxl import Workbook, load_workbook

from wikidisputes_ssot import annotation

HEADERS = [
    "dispute_sequence",
    "dispute_id",
    "dispute_label",
    "escalated",
    "utterance_order",
    "substantive_order",
    "utterance_role",
    "utterance_id",
    "original_utterance_id",
    "speaker_id",
    "timestamp",
    "reply_to_utterance_id",
    "reply_to_utterance_id_raw",
    "reply_to_utterance_order",
    "utterance_type",
    "source_page_title",
    "utterance_text",
    "wikipedia_revision_url",
    "dispute_resolution_url",
    "utterance_text_source",
]


def _gold_row(sequence: str, role: str, order: int, uid: str, text: str) -> list[object]:
    values: dict[str, object] = {
        "dispute_sequence": sequence,
        "dispute_id": f"dispute-{sequence}",
        "dispute_label": sequence,
        "escalated": 0,
        "utterance_order": order,
        "substantive_order": None if role == "context" else order - 1,
        "utterance_role": role,
        "utterance_id": uid,
        "original_utterance_id": uid,
        "speaker_id": "speaker",
        "timestamp": "2001-01-01T00:00:00+00:00",
        "utterance_type": "original",
        "source_page_title": sequence,
        "utterance_text": text,
        "utterance_text_source": "source",
    }
    return [values.get(header) for header in HEADERS]


ROWS = {
    "d1c": _gold_row("D01", "context", 1, "d1-context", "context one"),
    "d1u2": _gold_row("D01", "utterance", 2, "d1-u2", "old two"),
    "d1u3": _gold_row("D01", "utterance", 3, "d1-u3", "old three"),
    "d2c": _gold_row("D02", "context", 1, "d2-context", "context two"),
    "d2u2": _gold_row("D02", "utterance", 2, "d2-u2", "old four"),
    "d2u3": _gold_row("D02", "utterance", 3, "d2-u3", "old five"),
}


def _write_gold(path: Path, order: list[str]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Gold_Annotation"
    sheet.append(HEADERS)
    for key in order:
        sheet.append(ROWS[key])
    workbook.save(path)


def _read_gold(path: Path) -> list[dict[str, object]]:
    workbook = load_workbook(path, read_only=True, data_only=False)
    sheet = workbook["Gold_Annotation"]
    headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    return [
        dict(zip(headers, row, strict=True)) for row in sheet.iter_rows(min_row=2, values_only=True)
    ]


def test_gold_export_canonicalizes_physical_order_deterministically(
    tmp_path: Path, monkeypatch
) -> None:
    annotation_csv = tmp_path / "annotation.csv"
    fieldnames = [
        "dispute_id",
        "utterance_id",
        "utterance_role",
        "ssot_source_row_uid",
    ]
    with annotation_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for number, uid in enumerate(("d1-u2", "d1-u3", "d2-u2", "d2-u3"), start=2):
            writer.writerow(
                {
                    "dispute_id": f"dispute-D0{1 if number < 4 else 2}",
                    "utterance_id": uid,
                    "utterance_role": "utterance",
                    "ssot_source_row_uid": f"source-{uid}",
                }
            )

    selection = tmp_path / "selection.parquet"
    duckdb.sql(
        """
        COPY (
            SELECT * FROM (VALUES
                ('source-d1-u2', 'method_a', 'selected two'),
                ('source-d1-u3', 'method_b', 'selected three'),
                ('source-d2-u2', 'method_a_fallback', 'selected four'),
                ('source-d2-u3', 'method_a', 'selected five')
            ) AS t(source_row_uid, selected_method, selected_text)
        ) TO ? (FORMAT PARQUET)
        """,
        params=[str(selection)],
    )

    monkeypatch.setattr(annotation, "ANNOTATION", tmp_path / "output")
    monkeypatch.setattr(annotation, "FINAL_SELECTION", selection)
    first = tmp_path / "first.xlsx"
    second = tmp_path / "second.xlsx"
    _write_gold(first, ["d2u3", "d1u3", "d2c", "d1c", "d2u2", "d1u2"])
    _write_gold(second, ["d1u2", "d2c", "d2u2", "d1u3", "d1c", "d2u3"])

    annotation.export_annotation_ready_gold(first, annotation_csv)
    output = annotation.ANNOTATION / annotation.FINAL_GOLD_NAME
    first_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    first_rows = _read_gold(output)
    annotation.export_annotation_ready_gold(second, annotation_csv)
    second_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    second_rows = _read_gold(output)

    assert first_hash == second_hash
    assert first_rows == second_rows
    assert [row["utterance_id"] for row in first_rows] == [
        "d1-context",
        "d1-u2",
        "d1-u3",
        "d2-context",
        "d2-u2",
        "d2-u3",
    ]

    for sequence in ("D01", "D02"):
        dispute = [row for row in first_rows if row["dispute_sequence"] == sequence]
        assert dispute[0]["utterance_role"] == "context"
        orders = [
            int(row["utterance_order"]) for row in dispute if row["utterance_role"] == "utterance"
        ]
        assert orders == sorted(orders)
        assert all(left < right for left, right in pairwise(orders))

    assert Counter(
        (row["utterance_id"], row["utterance_text"], row["provenance"]) for row in first_rows
    ) == Counter(
        {
            ("d1-context", "context one", "context"): 1,
            ("d1-u2", "selected two", "method_a"): 1,
            ("d1-u3", "selected three", "method_b"): 1,
            ("d2-context", "context two", "context"): 1,
            ("d2-u2", "selected four", "method_a_fallback"): 1,
            ("d2-u3", "selected five", "method_a"): 1,
        }
    )


def test_gold_export_applies_only_configured_discussion_exclusions(
    tmp_path: Path, monkeypatch
) -> None:
    annotation_csv = tmp_path / "annotation.csv"
    with annotation_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "dispute_id",
                "dispute_label",
                "utterance_id",
                "original_utterance_id",
                "utterance_role",
                "ssot_source_row_uid",
                "ssot_episode_uid",
                "ssot_logical_utterance_uid",
            ],
        )
        writer.writeheader()
        for sequence, uid in (("D01", "d1-u2"), ("D02", "d2-u2")):
            writer.writerow(
                {
                    "dispute_id": f"dispute-{sequence}",
                    "dispute_label": sequence,
                    "utterance_id": uid,
                    "original_utterance_id": uid,
                    "utterance_role": "utterance",
                    "ssot_source_row_uid": f"source-{uid}",
                    "ssot_episode_uid": f"episode-{sequence}",
                    "ssot_logical_utterance_uid": f"logical-{uid}",
                }
            )

    selection = tmp_path / "selection.parquet"
    duckdb.sql(
        """
        COPY (
            SELECT * FROM (VALUES
                ('source-d1-u2', 'method_a', 'selected two'),
                ('source-d2-u2', 'method_a', 'selected four')
            ) AS t(source_row_uid, selected_method, selected_text)
        ) TO ? (FORMAT PARQUET)
        """,
        params=[str(selection)],
    )
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text(
        '{"exclusions":[{"dispute_id":"dispute-D02",'
        '"dispute_label":"D02","reason":"confirmed_malformed"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(annotation, "ANNOTATION", tmp_path / "output")
    monkeypatch.setattr(annotation, "FINAL_SELECTION", selection)
    monkeypatch.setattr(annotation, "ANNOTATION_EXCLUSIONS", exclusions)

    gold = tmp_path / "gold.xlsx"
    _write_gold(gold, ["d1c", "d1u2", "d2c", "d2u2"])
    report = annotation.export_annotation_ready_gold(gold, annotation_csv)
    rows = _read_gold(Path(report["path"]))

    assert {row["dispute_id"] for row in rows} == {"dispute-D01"}
    assert report["rows"] == 2
    assert report["excluded_discussions"][0]["reason"] == "confirmed_malformed"
