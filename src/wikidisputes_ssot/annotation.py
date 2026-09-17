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
        j.annotation_eligible AS annotation_eligible,
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
        u.chronology_eligible AS ssot_chronology_eligible,
        u.chronology_status AS ssot_chronology_status,
        u.chronology_rank AS ssot_chronology_rank,
        u.display_utterance_order AS canonical_display_utterance_order,
        u.display_order AS canonical_display_order,
        u.was_modified AS ssot_was_modified,
        u.recovery_status AS ssot_recovery_status,
        u.final_text_representation_uid,
        sourceact.action_type AS source_action_type,
        sourceact.raw_timestamp AS action_timestamp_raw,

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
            act.action_type,
            act.raw_timestamp
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


def _substantive_order_clause() -> str:
    """Return the total order used for annotation-facing substantive order.

    ``join_display_order`` is the canonical integrated order produced by the
    chronology pipeline: known creation times, deterministic equal-time ties,
    and constrained reply/action placement for unresolved rows.  Reusing it
    here preserves feasible-interval placement instead of moving every
    unresolved row after every known row.  The stable source keys make the
    result total without inferring a timestamp or an identity.
    """

    return """
        join_display_order NULLS LAST,
        source_order,
        source_row_uid
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
    WITH raw AS (
        {all_source_sql()}
    ),
    numbered AS (
        SELECT
            *,
            DENSE_RANK() OVER (
                ORDER BY episode_uid
            ) AS dispute_number
        FROM raw
    ),
    ordered AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY episode_uid
                ORDER BY
                    join_display_order NULLS LAST,
                    source_order,
                    source_row_uid
            ) AS local_display_order,

            ROW_NUMBER() OVER (
                PARTITION BY episode_uid
                ORDER BY
                    {_substantive_order_clause()}
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

        o.ssot_chronology_rank AS utterance_order,

        o.local_substantive_order AS substantive_order,

        -- Context remains canonical provenance, but it is not an
        -- annotation-facing exclusion or a special first-row role.
        'utterance' AS utterance_role,

        o.wikidisputes_current_id_exact AS utterance_id,
        o.wikidisputes_original_id_exact AS original_utterance_id,
        o.source_user_exact AS speaker_id,

        CAST(o.ssot_created_at_utc AS VARCHAR) AS timestamp,

        o.wikidisputes_reply_to_exact AS reply_to_utterance_id,
        o.wikidisputes_reply_to_exact AS reply_to_utterance_id_raw,
        target.chronology_rank AS reply_to_utterance_order,

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
        o.annotation_eligible,
        o.logical_utterance_uid AS ssot_logical_utterance_uid,
        o.context_node_uid AS ssot_context_node_uid,
        CASE
            WHEN o.context_node_uid IS NOT NULL THEN 'context'
            ELSE 'utterance'
        END AS ssot_row_provenance,
        o.episode_uid AS ssot_episode_uid,
        o.conversation_uid AS ssot_conversation_uid,

        o.ssot_chronology_eligible,
        o.ssot_chronology_status,
        o.ssot_chronology_rank,
        o.local_display_order AS display_order,
        o.canonical_display_utterance_order AS ssot_display_utterance_order,
        o.canonical_display_order AS ssot_canonical_display_order,
        o.ssot_created_at_status,
        o.wikidisputes_time AS ssot_raw_source_timestamp,
        o.action_timestamp_raw AS ssot_action_timestamp_raw,
        o.source_action_type AS ssot_action_type,
        CASE
            WHEN o.ssot_created_at_utc IS NOT NULL THEN 'creation_utc'
            ELSE 'creation_time_unresolved'
        END AS timestamp_semantics,
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

    LEFT JOIN (
        SELECT DISTINCT
            episode_uid,
            logical_utterance_uid,
            ssot_chronology_rank AS chronology_rank
        FROM raw
        WHERE logical_utterance_uid IS NOT NULL
    ) target
      ON target.episode_uid = o.episode_uid
     AND target.logical_utterance_uid = o.ssot_reply_target_logical_uid

    ORDER BY
        o.dispute_number,
        o.join_display_order NULLS LAST,
        o.source_order,
        o.source_row_uid
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
            COUNT(DISTINCT ssot_source_row_uid) AS distinct_source_rows,
            COUNT(*) FILTER (WHERE annotation_eligible) AS annotation_eligible_rows,
            COUNT(*) FILTER (
                WHERE utterance_role = 'utterance'
            ) AS utterance_rows,
            COUNT(*) FILTER (
                WHERE utterance_role = 'context'
            ) AS context_rows,
            COUNT(*) FILTER (
                WHERE ssot_context_node_uid IS NOT NULL
            ) AS context_classified_rows,
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
            SELECT ssot_source_row_uid, COUNT(*) AS n
            FROM x
            GROUP BY 1
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]

    if duplicate_count:
        raise RuntimeError(
            f"Full export contains {duplicate_count} duplicate source-row identities."
        )
    if counts["total_rows"] != 137_460 or counts["distinct_source_rows"] != 137_460:
        raise RuntimeError(
            "Full annotation export must retain all 137460 source rows; "
            f"rows={counts['total_rows']}; distinct source rows={counts['distinct_source_rows']}"
        )
    if counts["annotation_eligible_rows"] != 137_460:
        raise RuntimeError(
            "Every source row must be annotation-eligible; "
            f"eligible={counts['annotation_eligible_rows']}"
        )
    if counts["utterance_rows"] != 137_460 or counts["context_rows"] != 0:
        raise RuntimeError(
            "Every source row must have annotation-facing role 'utterance'; "
            f"utterances={counts['utterance_rows']}; contexts={counts['context_rows']}"
        )

    report = {
        "status": "pass",
        "annotation_csv": str(csv_path),
        "research_key_csv": str(research_key),
        **counts,
        "source_rows": counts["distinct_source_rows"],
        "annotation_eligible_rows": counts["annotation_eligible_rows"],
        "duplicate_source_rows": duplicate_count,
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
    """Match every source row as a codable annotation utterance.

    Older Gold shells can contain a descriptive ``context`` role.  It is not
    an annotation identity and is deliberately normalized here so the row is
    matched to the current, codable source-occurrence export.
    """

    return (
        str(row.get("dispute_id") or ""),
        str(row.get("utterance_id") or ""),
        "utterance",
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


def _numeric_order(value: Any, *, row_number: int) -> int | None:
    if value is None or value == "":
        return None
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


def _sort_gold_rows(
    sheet: Any,
    headers: list[str],
    display_orders: dict[int, int] | None = None,
) -> None:
    """Sort for display while preserving nullable chronology ranks in Gold."""

    header_index = {name: index + 1 for index, name in enumerate(headers)}
    records: list[tuple[tuple[Any, ...], list[dict[str, Any]]]] = []
    for row_number in range(2, sheet.max_row + 1):
        sequence = sheet.cell(row_number, header_index["dispute_sequence"]).value
        role = str(sheet.cell(row_number, header_index["utterance_role"]).value or "")
        if role not in {"context", "utterance"}:
            raise RuntimeError(f"Gold row {row_number} has invalid utterance_role {role!r}")
        chronology_rank = _numeric_order(
            sheet.cell(row_number, header_index["utterance_order"]).value,
            row_number=row_number,
        )
        display_order = (display_orders or {}).get(row_number)
        if display_order is None:
            display_order = chronology_rank
        if display_order is None:
            display_order = 2**63

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
                (
                    _natural_key(sequence),
                    display_order,
                    chronology_rank if chronology_rank is not None else 2**63,
                    str(sheet.cell(row_number, header_index["utterance_id"]).value or ""),
                ),
                cells,
            )
        )

    records.sort(key=lambda record: record[0])
    previous_sequence: str | None = None
    previous_rank: int | None = None
    for row_number, (_, cells) in enumerate(records, start=2):
        for column_number, state in enumerate(cells, start=1):
            cell = sheet.cell(row_number, column_number)
            cell.value = state["value"]
            cell._style = copy.copy(state["style"])
            cell.hyperlink = copy.copy(state["hyperlink"])
            cell.comment = copy.copy(state["comment"])

        sequence = str(sheet.cell(row_number, header_index["dispute_sequence"]).value)
        if sequence != previous_sequence:
            previous_sequence = sequence
            previous_rank = None
        chronology_rank = _numeric_order(
            sheet.cell(row_number, header_index["utterance_order"]).value,
            row_number=row_number,
        )
        if chronology_rank is not None:
            if previous_rank is not None and chronology_rank < previous_rank:
                raise RuntimeError(f"Gold dispute {sequence!r} has creation ranks out of order")
            previous_rank = chronology_rank


def export_annotation_ready_gold(gold_path: Path, annotation_csv: Path) -> dict[str, Any]:
    """Build the 20-column annotation shell plus one explicit provenance column."""

    workbook = load_workbook(gold_path)
    if "Gold_Annotation" not in workbook.sheetnames:
        raise RuntimeError("Gold workbook does not contain Gold_Annotation")
    sheet = workbook["Gold_Annotation"]
    source_headers = [str(cell.value) for cell in sheet[1]]
    if source_headers[-1:] == ["provenance"]:
        source_headers = source_headers[:-1]
    if len(source_headers) < EXPECTED_GOLD_COLUMNS:
        raise RuntimeError(
            "Gold input must contain at least the 20-column annotation shell; "
            f"found {sheet.max_column} columns"
        )
    headers = source_headers[:EXPECTED_GOLD_COLUMNS]
    if sheet.max_column > EXPECTED_GOLD_COLUMNS:
        sheet.delete_cols(
            EXPECTED_GOLD_COLUMNS + 1,
            sheet.max_column - EXPECTED_GOLD_COLUMNS,
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
    annotation_by_key: defaultdict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    annotation_by_identity: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    annotation_by_alias: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in annotation_rows:
        key = _gold_key(row)
        annotation_by_key[key].append(row)
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
    obsolete_context_rows: list[int] = []
    for row_number in range(2, sheet.max_row + 1):
        values = {
            headers[index - 1]: sheet.cell(row_number, index).value
            for index in range(1, len(headers) + 1)
        }
        key = _gold_key(values)
        key_candidates = annotation_by_key.get(key, [])
        match = None
        if key_candidates:
            source_uid = str(values.get("ssot_source_row_uid") or "")
            if source_uid:
                exact_source = [
                    candidate
                    for candidate in key_candidates
                    if candidate.get("ssot_source_row_uid") == source_uid
                ]
                if len(exact_source) == 1:
                    match = exact_source[0]
            if match is None:
                source_type = str(values.get("utterance_type") or "")
                matching_type = [
                    candidate
                    for candidate in key_candidates
                    if str(candidate.get("utterance_type") or "") == source_type
                ]
                if matching_type:
                    key_candidates = matching_type
                match = min(
                    key_candidates,
                    key=lambda candidate: (
                        str(candidate.get("utterance_type") or "") != "original",
                        int(candidate.get("display_order") or 2**63),
                        str(candidate.get("ssot_source_row_uid") or ""),
                    ),
                )
        if match is None:
            candidates: dict[tuple[str, str], dict[str, str]] = {}
            for alias in {values.get("utterance_id"), values.get("original_utterance_id")}:
                for candidate in annotation_by_alias.get((str(alias or ""), "utterance"), []):
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
            if str(values.get("utterance_role") or "") == "context":
                # Older shells carried a synthetic context scaffold which has
                # no source-occurrence counterpart.  It is neither canonical
                # provenance nor a codable row, so remove it rather than
                # requiring a first context row for the dispute.
                obsolete_context_rows.append(row_number)
                continue
            raise RuntimeError(f"Gold row {row_number} has no unique annotation match: {key}")
        # Source occurrence, rather than logical identity, is the annotation
        # unit.  Do not remove rows that share a canonical logical utterance.
        matched_by_row[row_number] = match

    for row_number in reversed(obsolete_context_rows):
        sheet.delete_rows(row_number)
        matched_by_row = {
            (number - 1 if number > row_number else number): match
            for number, match in matched_by_row.items()
        }

    counts: defaultdict[str, int] = defaultdict(int)
    substantive = context_classified_rows = 0
    display_orders_by_row: dict[int, int] = {}
    for row_number in range(2, sheet.max_row + 1):
        values = {
            headers[index - 1]: sheet.cell(row_number, index).value
            for index in range(1, len(headers) + 1)
        }
        match = matched_by_row[row_number]
        if match.get("display_order") not in (None, ""):
            display_orders_by_row[row_number] = int(match["display_order"])
        substantive += 1
        is_context_classified = bool(
            match.get("ssot_context_node_uid")
            or match.get("ssot_row_provenance") == "context"
            or match.get("utterance_role") == "context"
        )
        if is_context_classified:
            # Context classification remains provenance only.  Its exact
            # source text is codable, but it is not a Method-A/B promotion.
            provenance = "wikidisputes_source"
            sheet.cell(row_number, text_col, match.get("utterance_text") or "")
            context_classified_rows += 1
        else:
            selection = selected.get(match.get("ssot_source_row_uid", ""))
            provenance = selection[0] if selection else ""
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
                value: Any = match.get(field)
                if value not in (None, "") and field in {
                    "utterance_order",
                    "substantive_order",
                    "reply_to_utterance_order",
                }:
                    value = int(value)
                # ``Worksheet.cell(..., value=None)`` leaves an existing value
                # untouched.  Assign through ``Cell.value`` so canonical nulls
                # actually clear stale legacy ranks, timestamps, and IDs.
                sheet.cell(row_number, headers.index(field) + 1).value = (
                    None if value in (None, "") else value
                )
        # The role is annotation-facing rather than canonical provenance.
        sheet.cell(row_number, headers.index("utterance_role") + 1, "utterance")
        sheet.cell(row_number, provenance_col, provenance)
        sheet.cell(row_number, provenance_col)._style = copy.copy(
            sheet.cell(row_number, len(headers))._style
        )
        counts[provenance] += 1

    _sort_gold_rows(sheet, [*headers, "provenance"], display_orders_by_row)

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
        "context_rows": 0,
        "context_classified_rows": context_classified_rows,
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
        "contract_version": "annotation-export-v2-all-source-rows-utterances",
        "validation_decision": _accepted_decision(),
        "annotation": annotation_report,
        "gold": gold_report,
        "artifacts": artifacts,
    }
    atomic_write_json(ANNOTATION / "annotation_manifest.json", manifest)
    return manifest
