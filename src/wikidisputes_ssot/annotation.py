from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb
from openpyxl import load_workbook

from .io import atomic_write_bytes, atomic_write_json

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output"
CANONICAL = OUTPUT / "canonical"
SILVER = OUTPUT / "silver"
ANNOTATION = OUTPUT / "annotation"
REPORTS = OUTPUT / "reports"
FINAL_SELECTION = SILVER / "method_b_combined_representation.parquet"
VALIDATION_DECISION = ROOT / "config" / "decisions" / "method_b_validation_decision.json"
ANNOTATION_EXCLUSIONS = ROOT / "config" / "decisions" / "annotation_exclusions.json"
FINAL_GOLD_NAME = "gold_input_ssot_annotation_ready.xlsx"
EXPECTED_GOLD_COLUMNS = 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _accepted_decision() -> dict[str, Any]:
    decision = json.loads(VALIDATION_DECISION.read_text(encoding="utf-8"))
    if decision.get("method_b_accepted") is not True:
        raise RuntimeError("Method-B selection has not been accepted")
    return decision


def _annotation_exclusions() -> list[dict[str, str]]:
    payload = json.loads(ANNOTATION_EXCLUSIONS.read_text(encoding="utf-8"))
    exclusions = payload.get("exclusions")
    if not isinstance(exclusions, list):
        raise RuntimeError("annotation exclusions must contain an exclusions list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for exclusion in exclusions:
        dispute_id = str(exclusion.get("dispute_id") or "")
        label = str(exclusion.get("dispute_label") or "")
        reason = str(exclusion.get("reason") or "")
        if not dispute_id or not label or not reason or dispute_id in seen:
            raise RuntimeError(f"invalid annotation exclusion: {exclusion!r}")
        seen.add(dispute_id)
        result.append({"dispute_id": dispute_id, "dispute_label": label, "reason": reason})
    return result


def qpath(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def setup(con: duckdb.DuckDBPyConnection) -> None:
    files = {
        "j": SILVER / "annotation_join_contract.parquet",
        "sp": CANONICAL / "wikidisputes_source_projection.parquet",
        "u": CANONICAL / "wikidisputes_utterances_ssot.parquet",
        "r": SILVER / "utterance_representations.parquet",
        "a": SILVER / "utterance_actions.parquet",
        "disp": CANONICAL / "wikidisputes_annotation_display.parquet",
        "re": SILVER / "reply_edges.parquet",
        "mwr": SILVER / "mediawiki_raw_comment_representations.parquet",
    }

    missing = [str(p) for p in files.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Required completed SSOT outputs are missing:\n" + "\n".join(missing)
        )

    for name, path in files.items():
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{qpath(path)}')")


def all_source_sql() -> str:
    return """
    SELECT
        j.join_row_uid,
        j.source_row_uid,
        j.dispute_uid,
        j.episode_uid,
        j.conversation_uid,
        j.logical_utterance_uid,
        j.context_node_uid,
        j.utterance_order AS join_utterance_order,
        j.display_order AS join_display_order,

        j.wikidisputes_current_id_exact,
        j.wikidisputes_original_id_exact,
        j.wikidisputes_text_exact AS source_text_exact,
        COALESCE(
            u.wikiconv_speaker_exact,
            u.wikidisputes_user_exact,
            j.wikidisputes_user_exact
        ) AS source_user_exact,

        sp.source_case_index,
        sp.source_row_index,
        sp.source_order,
        sp.source_dispute_id_exact,
        sp.source_wikidisputes_escalated,
        sp.wikidisputes_conv_id_exact,
        sp.wikidisputes_reply_to_exact,
        sp.wikidisputes_time,
        sp.wikidisputes_type_exact,
        sp.wikidisputes_pagetitle_exact,

        MAX(sp.wikidisputes_pagetitle_exact)
            OVER (PARTITION BY j.episode_uid) AS episode_page_title,

        u.created_at_utc AS ssot_created_at_utc,
        u.created_at_status AS ssot_created_at_status,
        u.utterance_order AS ssot_utterance_order,
        u.was_modified AS ssot_was_modified,
        u.recovery_status AS ssot_recovery_status,
        u.final_text_representation_uid,

        re.raw_reply_target AS ssot_raw_reply_target,
        re.target_logical_utterance_uid AS ssot_reply_target_logical_uid,
        re.target_utterance_order AS ssot_reply_target_utterance_order,
        re.resolution_status AS ssot_reply_resolution_status,

        CASE
            WHEN j.context_node_uid IS NOT NULL THEN
                COALESCE(
                    NULLIF(dc.text_exact, ''),
                    CASE WHEN NULLIF(TRIM(j.wikidisputes_text_exact), '') IS NOT NULL THEN j.wikidisputes_text_exact END
                )
            ELSE
                COALESCE(

                    CASE WHEN NULLIF(TRIM(mwbody.content_inline), '') IS NOT NULL THEN mwbody.content_inline END,
                    CASE WHEN NULLIF(TRIM(finalr.content_inline), '') IS NOT NULL THEN finalr.content_inline END,
                    CASE WHEN NULLIF(TRIM(fallbackr.content_inline), '') IS NOT NULL THEN fallbackr.content_inline END,
                    CASE WHEN NULLIF(TRIM(du.text_exact), '') IS NOT NULL THEN du.text_exact END,
                    CASE WHEN NULLIF(TRIM(j.wikidisputes_text_exact), '') IS NOT NULL THEN j.wikidisputes_text_exact END
                )
        END AS annotation_text,

        CASE
            WHEN j.context_node_uid IS NOT NULL
                THEN 'context_exact'
            WHEN CASE WHEN NULLIF(TRIM(mwbody.content_inline), '') IS NOT NULL THEN mwbody.content_inline END IS NOT NULL
                THEN 'mediawiki_revision_comment_wikitext_body'
            WHEN CASE WHEN NULLIF(TRIM(finalr.content_inline), '') IS NOT NULL THEN finalr.content_inline END IS NOT NULL
                THEN 'wikiconv_final_text_exact'
            WHEN CASE WHEN NULLIF(TRIM(fallbackr.content_inline), '') IS NOT NULL THEN fallbackr.content_inline END IS NOT NULL
                THEN fallbackr.representation_kind
            WHEN CASE WHEN NULLIF(TRIM(du.text_exact), '') IS NOT NULL THEN du.text_exact END IS NOT NULL
                THEN 'annotation_display_exact'
            ELSE 'wikidisputes_text_exact'
        END AS annotation_text_source

    FROM j
    JOIN sp
      ON sp.source_row_uid = j.source_row_uid

    LEFT JOIN u
      ON u.logical_utterance_uid = j.logical_utterance_uid

    LEFT JOIN LATERAL (
        SELECT
            act.version_uid,
            act.action_uid,
            act.action_type
        FROM a act
        WHERE act.logical_utterance_uid = j.logical_utterance_uid
          AND CAST(act.action_id_exact AS VARCHAR)
              = CAST(sp.wikidisputes_id_exact AS VARCHAR)
        ORDER BY
            CASE
                WHEN act.source_row_uid = j.source_row_uid THEN 0
                ELSE 1
            END,
            act.action_uid
        LIMIT 1
    ) sourceact ON TRUE

    LEFT JOIN LATERAL (
        SELECT
            rr.content_inline,
            rr.representation_uid,
            rr.confidence
        FROM r rr
        WHERE rr.logical_utterance_uid = j.logical_utterance_uid
          AND rr.version_uid = sourceact.version_uid
          AND rr.representation_kind = 'utterance_wikitext_fragment'
          AND rr.availability_status = 'recovered'
          AND NULLIF(rr.content_inline, '') IS NOT NULL
        ORDER BY rr.representation_uid
        LIMIT 1
    ) sourcefrag ON TRUE



    /* Historical raw MediaWiki body that passed the independent
       promotion-safety gate for this exact source occurrence.
       V3.3 high-confidence alone is not sufficient. */
    LEFT JOIN LATERAL (
        SELECT
            rr.content_inline,
            rr.representation_uid,
            rr.confidence,
            rr.source_revision_id,
            rr.best_similarity,
            rr.match_margin
        FROM mwr rr
        WHERE rr.logical_utterance_uid =
              j.logical_utterance_uid
          AND rr.source_row_uid =
              j.source_row_uid
          AND rr.representation_kind =
              'mediawiki_revision_comment_wikitext_body'
          AND rr.availability_status =
              'recovered'
          AND rr.confidence =
              'high_confidence_comment_match'
          AND rr.promotion_safety_decision =
              'promote'
          AND NULLIF(
                  TRIM(rr.content_inline),
                  ''
              ) IS NOT NULL
        ORDER BY rr.representation_uid
        LIMIT 1
    ) mwbody ON TRUE
    LEFT JOIN r finalr
      ON finalr.representation_uid = u.final_text_representation_uid

    LEFT JOIN LATERAL (
        SELECT
            rr.content_inline,
            rr.representation_kind
        FROM r rr
        WHERE rr.logical_utterance_uid = j.logical_utterance_uid
          AND NULLIF(rr.content_inline, '') IS NOT NULL
          AND rr.representation_kind IN (
              'wikiconv_action_text_exact',
              'wikidisputes_text_exact'
          )
        ORDER BY
            CASE
                WHEN rr.representation_kind =
                     'wikiconv_action_text_exact' THEN 0
                ELSE 1
            END,
            rr.available_at DESC NULLS LAST,
            rr.representation_uid
        LIMIT 1
    ) fallbackr ON TRUE

    LEFT JOIN re
      ON re.source_logical_utterance_uid = j.logical_utterance_uid

    LEFT JOIN disp du
      ON du.logical_utterance_uid = j.logical_utterance_uid
     AND du.row_kind = 'utterance'

    LEFT JOIN disp dc
      ON dc.context_node_uid = j.context_node_uid
     AND dc.row_kind = 'context'
    """


def entity_sql() -> str:
    return f"""
    WITH raw AS (
        {all_source_sql()}
    ),
    ranked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY
                    episode_uid,
                    COALESCE(logical_utterance_uid, context_node_uid)
                ORDER BY
                    CASE
                        WHEN context_node_uid IS NOT NULL THEN 0
                        WHEN wikidisputes_type_exact = 'original' THEN 0
                        ELSE 1
                    END,
                    source_order,
                    source_row_uid
            ) AS entity_rank
        FROM raw
        WHERE logical_utterance_uid IS NOT NULL
           OR context_node_uid IS NOT NULL
    )
    SELECT * EXCLUDE(entity_rank)
    FROM ranked
    WHERE entity_rank = 1
    """


def full_export_sql() -> str:
    return f"""
    WITH base AS (
        {entity_sql()}
    ),
    numbered AS (
        SELECT
            *,
            DENSE_RANK() OVER (
                ORDER BY episode_uid
            ) AS dispute_number
        FROM base
    ),
    ordered AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY episode_uid
                ORDER BY
                    CASE WHEN context_node_uid IS NOT NULL THEN 0 ELSE 1 END,
                    ssot_utterance_order NULLS LAST,
                    join_display_order NULLS LAST,
                    source_order,
                    COALESCE(logical_utterance_uid, context_node_uid)
            ) AS local_order,

            SUM(
                CASE WHEN logical_utterance_uid IS NOT NULL THEN 1 ELSE 0 END
            ) OVER (
                PARTITION BY episode_uid
                ORDER BY
                    CASE WHEN context_node_uid IS NOT NULL THEN 0 ELSE 1 END,
                    ssot_utterance_order NULLS LAST,
                    join_display_order NULLS LAST,
                    source_order,
                    COALESCE(logical_utterance_uid, context_node_uid)
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS local_substantive_order
        FROM numbered
    )
    SELECT
        'D' || LPAD(CAST(o.dispute_number AS VARCHAR), 5, '0')
            AS dispute_sequence,

        COALESCE(
            o.source_dispute_id_exact,
            o.wikidisputes_conv_id_exact,
            o.episode_uid
        ) AS dispute_id,

        o.episode_page_title AS dispute_label,

        o.local_order AS utterance_order,

        CASE
            WHEN o.logical_utterance_uid IS NULL THEN NULL
            ELSE o.local_substantive_order
        END AS substantive_order,

        CASE
            WHEN o.context_node_uid IS NOT NULL THEN 'context'
            ELSE 'utterance'
        END AS utterance_role,

        o.wikidisputes_current_id_exact AS utterance_id,
        o.wikidisputes_original_id_exact AS original_utterance_id,
        o.source_user_exact AS speaker_id,

        CASE
            WHEN o.logical_utterance_uid IS NOT NULL
                THEN COALESCE(
                    CAST(o.ssot_created_at_utc AS VARCHAR),
                    o.wikidisputes_time
                )
            ELSE o.wikidisputes_time
        END AS timestamp,

        o.wikidisputes_reply_to_exact AS reply_to_utterance_id,
        o.wikidisputes_reply_to_exact AS reply_to_utterance_id_raw,
        target.local_order AS reply_to_utterance_order,

        o.wikidisputes_type_exact AS utterance_type,
        o.episode_page_title AS source_page_title,
        o.annotation_text AS utterance_text,

        CASE
            WHEN o.wikidisputes_current_id_exact IS NULL THEN NULL
            ELSE
                'https://en.wikipedia.org/w/index.php?oldid=' ||
                split_part(o.wikidisputes_current_id_exact, '.', 1)
        END AS wikipedia_revision_url,

        o.source_row_uid AS ssot_source_row_uid,
        o.logical_utterance_uid AS ssot_logical_utterance_uid,
        o.context_node_uid AS ssot_context_node_uid,
        o.episode_uid AS ssot_episode_uid,
        o.conversation_uid AS ssot_conversation_uid,

        o.ssot_utterance_order,
        o.join_display_order AS ssot_display_order,
        o.ssot_created_at_status,
        o.ssot_reply_target_logical_uid,
        o.ssot_reply_target_utterance_order,
        o.ssot_reply_resolution_status,

        o.annotation_text_source AS ssot_annotation_text_source,
        o.source_text_exact AS ssot_source_text_exact,

        CASE
            WHEN o.annotation_text IS DISTINCT FROM o.source_text_exact
                THEN TRUE
            ELSE FALSE
        END AS ssot_text_differs_from_source,

        o.ssot_was_modified,
        o.ssot_recovery_status

    FROM ordered o

    LEFT JOIN ordered target
      ON target.episode_uid = o.episode_uid
     AND target.logical_utterance_uid =
         o.ssot_reply_target_logical_uid

    ORDER BY
        o.dispute_number,
        CASE WHEN o.context_node_uid IS NOT NULL THEN 0 ELSE 1 END,
        o.ssot_utterance_order NULLS LAST,
        o.join_display_order NULLS LAST,
        o.source_order,
        COALESCE(o.logical_utterance_uid, o.context_node_uid)
    """


def dict_rows(
    con: duckdb.DuckDBPyConnection,
    query: str,
) -> list[dict[str, Any]]:
    cur = con.execute(query)
    names = [x[0] for x in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def export_full(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    ANNOTATION.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    csv_path = ANNOTATION / "wikidisputes_llm_annotation_input.csv"
    research_key = ANNOTATION / "wikidisputes_annotation_research_key.csv"
    _accepted_decision()

    query = full_export_sql()

    con.execute(f"COPY ({query}) TO '{qpath(csv_path)}' (FORMAT CSV, HEADER, DELIMITER ',')")

    selected = {
        str(row[0]): (str(row[1]), row[2])
        for row in con.execute(
            "SELECT source_row_uid, selected_method, selected_text "
            f"FROM read_parquet('{qpath(FINAL_SELECTION)}')"
        ).fetchall()
    }
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise RuntimeError("annotation export has no header")
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=reader.fieldnames, lineterminator="\n")
        writer.writeheader()
        method_b_rows = 0
        for row in reader:
            selection = selected.get(row.get("ssot_source_row_uid", ""))
            if selection and selection[0] == "method_b":
                row["utterance_text"] = "" if selection[1] is None else str(selection[1])
                row["ssot_annotation_text_source"] = "mediawiki_revision_diff_comment_wikitext_body"
                method_b_rows += 1
            writer.writerow(row)
    final_bytes = buffer.getvalue().encode("utf-8")
    atomic_write_bytes(csv_path, final_bytes)

    con.execute(
        f"""
        COPY (
            SELECT DISTINCT
                episode_uid AS ssot_episode_uid,
                dispute_uid AS ssot_dispute_uid,
                source_wikidisputes_escalated AS escalated
            FROM ({entity_sql()})
            ORDER BY ssot_episode_uid
        )
        TO '{qpath(research_key)}'
        (FORMAT CSV, HEADER, DELIMITER ',')
        """
    )

    counts = dict_rows(
        con,
        f"""
        WITH x AS ({query})
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (
                WHERE utterance_role = 'utterance'
            ) AS utterance_rows,
            COUNT(*) FILTER (
                WHERE utterance_role = 'context'
            ) AS context_rows,
            COUNT(DISTINCT ssot_episode_uid) AS disputes,
            COUNT(DISTINCT ssot_logical_utterance_uid) FILTER (
                WHERE utterance_role = 'utterance'
            ) AS distinct_logical_utterances,
            COUNT(*) FILTER (
                WHERE utterance_role = 'utterance'
                  AND (
                      utterance_text IS NULL
                      OR LENGTH(TRIM(utterance_text)) = 0
                  )
            ) AS empty_utterance_text
        FROM x
        """,
    )[0]

    duplicate_count = con.execute(
        f"""
        WITH x AS ({query})
        SELECT COUNT(*)
        FROM (
            SELECT
                ssot_episode_uid,
                ssot_logical_utterance_uid,
                COUNT(*) AS n
            FROM x
            WHERE utterance_role = 'utterance'
            GROUP BY 1, 2
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]

    if duplicate_count:
        raise RuntimeError(
            f"Full export contains {duplicate_count} duplicate episode/logical-utterance pairs."
        )

    report = {
        "status": "pass",
        "annotation_csv": str(csv_path),
        "research_key_csv": str(research_key),
        **counts,
        "duplicate_episode_logical_pairs": duplicate_count,
        "outcome_columns_in_annotation_csv": [],
        "method_b_rows": method_b_rows,
        "selection_artifact": str(FINAL_SELECTION),
    }

    (REPORTS / "annotation_export_report.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    return report


def _gold_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("dispute_id") or ""),
        str(row.get("utterance_id") or ""),
        str(row.get("utterance_role") or ""),
    )


def _workbook_values(path: Path) -> list[tuple[str, list[list[Any]]]]:
    workbook = load_workbook(path, data_only=False)
    return [
        (sheet.title, [[cell.value for cell in row] for row in sheet.iter_rows()])
        for sheet in workbook.worksheets
    ]


def _natural_key(value: Any) -> tuple[tuple[int, int | str], ...]:
    """Return a deterministic ascending key for identifiers such as D01 or D12."""

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(value or ""))
        if part
    )


def _numeric_order(value: Any, *, row_number: int) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"Gold row {row_number} has non-numeric utterance_order {value!r}")
    try:
        numeric = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f"Gold row {row_number} has non-numeric utterance_order {value!r}"
        ) from error
    if numeric < 1 or numeric != value:
        raise RuntimeError(f"Gold row {row_number} has invalid utterance_order {value!r}")
    return numeric


def _sort_gold_rows(sheet: Any, headers: list[str]) -> None:
    """Physically sort Gold rows without changing their canonical order fields."""

    header_index = {name: index + 1 for index, name in enumerate(headers)}
    records: list[tuple[tuple[Any, ...], list[dict[str, Any]]]] = []
    context_counts: defaultdict[str, int] = defaultdict(int)

    for row_number in range(2, sheet.max_row + 1):
        sequence = sheet.cell(row_number, header_index["dispute_sequence"]).value
        role = str(sheet.cell(row_number, header_index["utterance_role"]).value or "")
        if role not in {"context", "utterance"}:
            raise RuntimeError(f"Gold row {row_number} has invalid utterance_role {role!r}")
        if role == "context":
            context_counts[str(sequence)] += 1
            role_order = 0
            utterance_order = 0
        else:
            role_order = 1
            utterance_order = _numeric_order(
                sheet.cell(row_number, header_index["utterance_order"]).value,
                row_number=row_number,
            )

        cells = []
        for cell in sheet[row_number]:
            cells.append(
                {
                    "value": cell.value,
                    "style": copy.copy(cell._style),
                    "hyperlink": copy.copy(cell.hyperlink),
                    "comment": copy.copy(cell.comment),
                }
            )
        records.append(
            (
                (_natural_key(sequence), role_order, utterance_order),
                cells,
            )
        )

    disputes = {
        str(sheet.cell(row, header_index["dispute_sequence"]).value)
        for row in range(2, sheet.max_row + 1)
    }
    invalid_contexts = {
        dispute: context_counts[dispute] for dispute in disputes if context_counts[dispute] != 1
    }
    if invalid_contexts:
        raise RuntimeError(
            f"Gold must contain exactly one context row per dispute; found {invalid_contexts}"
        )

    records.sort(key=lambda record: record[0])
    previous_sequence: str | None = None
    previous_order = 0
    for row_number, (_, cells) in enumerate(records, start=2):
        for column_number, state in enumerate(cells, start=1):
            cell = sheet.cell(row_number, column_number)
            cell.value = state["value"]
            cell._style = copy.copy(state["style"])
            cell.hyperlink = copy.copy(state["hyperlink"])
            cell.comment = copy.copy(state["comment"])

        sequence = str(sheet.cell(row_number, header_index["dispute_sequence"]).value)
        role = str(sheet.cell(row_number, header_index["utterance_role"]).value)
        if sequence != previous_sequence:
            if role != "context":
                raise RuntimeError(f"Gold dispute {sequence!r} does not start with context")
            previous_sequence = sequence
            previous_order = 0
        elif role == "context":
            raise RuntimeError(f"Gold dispute {sequence!r} contains a misplaced context row")
        else:
            order = _numeric_order(
                sheet.cell(row_number, header_index["utterance_order"]).value,
                row_number=row_number,
            )
            if order <= previous_order:
                raise RuntimeError(f"Gold dispute {sequence!r} has non-increasing utterance_order")
            previous_order = order


def export_annotation_ready_gold(gold_path: Path, annotation_csv: Path) -> dict[str, Any]:
    """Build the 20-column annotation shell plus one explicit provenance column."""

    workbook = load_workbook(gold_path)
    if "Gold_Annotation" not in workbook.sheetnames:
        raise RuntimeError("Gold workbook does not contain Gold_Annotation")
    sheet = workbook["Gold_Annotation"]
    headers = [str(cell.value) for cell in sheet[1]]
    if headers[-1:] == ["provenance"]:
        headers = headers[:-1]
    if len(headers) != EXPECTED_GOLD_COLUMNS:
        raise RuntimeError(
            "Gold input must contain the 20-column shell with optional provenance; "
            f"found {sheet.max_column} columns"
        )
    forbidden = [name for name in headers if name.startswith("ssot_") or name.endswith("_legacy")]
    if forbidden:
        raise RuntimeError(f"engineering columns are forbidden in annotation Gold: {forbidden}")
    required = {
        "dispute_sequence",
        "dispute_id",
        "utterance_order",
        "utterance_id",
        "utterance_role",
        "utterance_text",
    }
    if missing := sorted(required - set(headers)):
        raise RuntimeError(f"Gold input is missing required columns: {missing}")

    exclusions = _annotation_exclusions()
    excluded_ids = {row["dispute_id"] for row in exclusions}
    dispute_id_col = headers.index("dispute_id") + 1
    for row_number in range(sheet.max_row, 1, -1):
        if str(sheet.cell(row_number, dispute_id_col).value or "") in excluded_ids:
            sheet.delete_rows(row_number)

    with annotation_csv.open("r", encoding="utf-8", newline="") as handle:
        annotation_rows = list(csv.DictReader(handle))
    annotation_by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    annotation_by_identity: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    annotation_by_alias: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in annotation_rows:
        key = _gold_key(row)
        if key in annotation_by_key:
            raise RuntimeError(f"non-unique annotation identity for Gold key {key}")
        annotation_by_key[key] = row
        annotation_by_identity[
            (str(row.get("utterance_id") or ""), str(row.get("utterance_role") or ""))
        ].append(row)
        for alias in {row.get("utterance_id", ""), row.get("original_utterance_id", "")}:
            if alias:
                annotation_by_alias[(str(alias), str(row.get("utterance_role") or ""))].append(row)

    selected = {
        str(row[0]): (str(row[1]), "" if row[2] is None else str(row[2]))
        for row in duckdb.sql(
            "SELECT source_row_uid, selected_method, selected_text "
            f"FROM read_parquet('{qpath(FINAL_SELECTION)}')"
        ).fetchall()
    }
    text_col = headers.index("utterance_text") + 1
    provenance_col = len(headers) + 1
    sheet.cell(1, provenance_col, "provenance")
    sheet.cell(1, provenance_col)._style = copy.copy(sheet.cell(1, len(headers))._style)
    sheet.column_dimensions[sheet.cell(1, provenance_col).column_letter].width = 22

    matched_by_row: dict[int, dict[str, str]] = {}
    duplicate_rows: list[int] = []
    seen_logical: set[tuple[str, str]] = set()
    for row_number in range(2, sheet.max_row + 1):
        values = {
            headers[index - 1]: sheet.cell(row_number, index).value
            for index in range(1, len(headers) + 1)
        }
        role = str(values.get("utterance_role") or "")
        if role == "context":
            continue
        key = _gold_key(values)
        match = annotation_by_key.get(key)
        if match is None:
            candidates: dict[tuple[str, str], dict[str, str]] = {}
            for alias in {values.get("utterance_id"), values.get("original_utterance_id")}:
                for candidate in annotation_by_alias.get((str(alias or ""), role), []):
                    candidate_key = (
                        str(candidate.get("ssot_episode_uid") or ""),
                        str(candidate.get("ssot_logical_utterance_uid") or ""),
                    )
                    candidates[candidate_key] = candidate
            same_label = [
                row
                for row in candidates.values()
                if str(row.get("dispute_label") or "") == str(values.get("dispute_label") or "")
            ]
            if len(same_label) == 1:
                match = same_label[0]
            elif len(candidates) == 1:
                match = next(iter(candidates.values()))
        if match is None:
            candidates = annotation_by_identity.get((key[1], key[2]), [])
            if len(candidates) == 1:
                match = candidates[0]
        if match is None:
            raise RuntimeError(f"Gold row {row_number} has no unique annotation match: {key}")
        logical_key = (
            str(match.get("ssot_episode_uid") or match.get("dispute_id") or ""),
            str(match.get("ssot_logical_utterance_uid") or match.get("utterance_id") or ""),
        )
        if logical_key in seen_logical:
            duplicate_rows.append(row_number)
        else:
            seen_logical.add(logical_key)
            matched_by_row[row_number] = match

    for row_number in reversed(duplicate_rows):
        sheet.delete_rows(row_number)
        matched_by_row = {
            (number - 1 if number > row_number else number): match
            for number, match in matched_by_row.items()
        }

    counts: defaultdict[str, int] = defaultdict(int)
    substantive = context = 0
    for row_number in range(2, sheet.max_row + 1):
        values = {
            headers[index - 1]: sheet.cell(row_number, index).value
            for index in range(1, len(headers) + 1)
        }
        key = _gold_key(values)
        role = str(values.get("utterance_role") or "")
        if role == "context":
            provenance = "context"
            context += 1
        else:
            match = matched_by_row[row_number]
            selection = selected.get(match.get("ssot_source_row_uid", ""))
            provenance = selection[0] if selection else ""
            substantive += 1
            if provenance not in {"method_a", "method_b", "method_a_fallback"}:
                raise RuntimeError(f"Gold row {row_number} has invalid provenance {provenance!r}")
            sheet.cell(row_number, text_col, selection[1])
            for field in (
                "utterance_order",
                "substantive_order",
                "utterance_id",
                "original_utterance_id",
                "speaker_id",
                "timestamp",
                "reply_to_utterance_id",
                "reply_to_utterance_id_raw",
                "reply_to_utterance_order",
                "utterance_type",
                "wikipedia_revision_url",
            ):
                if field in headers and field in match:
                    value: Any = match.get(field) or None
                    if value is not None and field in {
                        "utterance_order",
                        "substantive_order",
                        "reply_to_utterance_order",
                    }:
                        value = int(value)
                    sheet.cell(row_number, headers.index(field) + 1, value)
        sheet.cell(row_number, provenance_col, provenance)
        sheet.cell(row_number, provenance_col)._style = copy.copy(
            sheet.cell(row_number, len(headers))._style
        )
        counts[provenance] += 1

    _sort_gold_rows(sheet, [*headers, "provenance"])

    total = sheet.max_row - 1
    ANNOTATION.mkdir(parents=True, exist_ok=True)
    output_path = ANNOTATION / FINAL_GOLD_NAME
    with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=ANNOTATION, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        workbook.save(temporary)
        if not output_path.exists() or _workbook_values(output_path) != _workbook_values(temporary):
            temporary.replace(output_path)
        else:
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": str(output_path),
        "sha256": _sha256(output_path),
        "rows": total,
        "substantive_rows": substantive,
        "context_rows": context,
        "columns": len(headers) + 1,
        "headers": [*headers, "provenance"],
        "provenance": dict(sorted(counts.items())),
        "excluded_discussions": exclusions,
    }


def export_annotation_bundle(gold_path: Path) -> dict[str, Any]:
    """Build and validate all supported annotation deliverables."""

    connection = duckdb.connect()
    try:
        setup(connection)
        annotation_report = export_full(connection)
    finally:
        connection.close()
    gold_report = export_annotation_ready_gold(gold_path, Path(annotation_report["annotation_csv"]))
    artifacts = {}
    for path in (
        ANNOTATION / "wikidisputes_llm_annotation_input.csv",
        ANNOTATION / "wikidisputes_annotation_research_key.csv",
        ANNOTATION / FINAL_GOLD_NAME,
    ):
        artifacts[path.name] = {"path": str(path), "sha256": _sha256(path)}
    manifest = {
        "status": "pass",
        "contract_version": "annotation-export-v1-method-b-final",
        "validation_decision": _accepted_decision(),
        "annotation": annotation_report,
        "gold": gold_report,
        "artifacts": artifacts,
    }
    atomic_write_json(ANNOTATION / "annotation_manifest.json", manifest)
    return manifest
