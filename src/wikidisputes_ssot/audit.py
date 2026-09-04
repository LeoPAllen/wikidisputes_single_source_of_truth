from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb

from .constants import CROSS_LABEL_DISCUSSION_IDS, EXPECTED_COUNTS
from .hashing import sha256_bytes
from .io import atomic_write_json


def _normalized_words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.findall(r"\w+", normalized, flags=re.UNICODE)


def _simhash64(words: list[str]) -> int:
    features = {" ".join(words[index : index + 3]) for index in range(len(words) - 2)}
    vector = [0] * 64
    for feature in sorted(features)[:2048]:
        value = int.from_bytes(
            hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for bit in range(64):
            vector[bit] += 1 if value & (1 << bit) else -1
    result = 0
    for bit, score in enumerate(vector):
        if score >= 0:
            result |= 1 << bit
    return result


def _near_duplicates(rows: list[tuple[str, str | None]], limit: int = 1000) -> list[dict[str, Any]]:
    bands: dict[tuple[int, int], list[tuple[str, int, str]]] = defaultdict(list)
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for source_uid, text in rows:
        words = _normalized_words(text or "")
        if len(words) < 5:
            continue
        normalized = " ".join(words)
        fingerprint = _simhash64(words)
        for band in range(4):
            key = (band, (fingerprint >> (band * 16)) & 0xFFFF)
            # Limit pathological generic buckets without changing canonical data.
            for prior_uid, prior_fingerprint, prior_normalized in bands[key][-200:]:
                if prior_normalized == normalized:
                    continue
                distance = (fingerprint ^ prior_fingerprint).bit_count()
                if distance <= 3:
                    pair = tuple(sorted((source_uid, prior_uid)))
                    candidates[pair] = {
                        "source_row_uid_a": pair[0],
                        "source_row_uid_b": pair[1],
                        "simhash64_hamming_distance": distance,
                    }
                    if len(candidates) >= limit:
                        return sorted(
                            candidates.values(),
                            key=lambda row: (
                                row["simhash64_hamming_distance"],
                                row["source_row_uid_a"],
                                row["source_row_uid_b"],
                            ),
                        )
            bands[key].append((source_uid, fingerprint, normalized))
    return sorted(
        candidates.values(),
        key=lambda row: (
            row["simhash64_hamming_distance"],
            row["source_row_uid_a"],
            row["source_row_uid_b"],
        ),
    )


def audit_source(projection_path: Path, report_root: Path) -> dict[str, Any]:
    report_root.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.read_parquet(str(projection_path)).create_view("source_rows")

    by_side = dict(
        connection.execute(
            "SELECT source_side, count(*) FROM source_rows "
            "GROUP BY source_side ORDER BY source_side"
        ).fetchall()
    )
    type_counts = dict(
        connection.execute(
            "SELECT wikidisputes_type_exact, count(*) FROM source_rows "
            "GROUP BY wikidisputes_type_exact ORDER BY wikidisputes_type_exact"
        ).fetchall()
    )
    discussion_counts = dict(
        connection.execute(
            "SELECT source_side, count(DISTINCT source_case_uid) FROM source_rows "
            "GROUP BY source_side ORDER BY source_side"
        ).fetchall()
    )
    total_rows, unique_ids = connection.execute(
        "SELECT count(*), count(DISTINCT wikidisputes_id_exact) FROM source_rows"
    ).fetchone()
    raw_dangling = connection.execute(
        """
        SELECT count(*)
        FROM source_rows child
        LEFT JOIN source_rows parent
          ON parent.source_case_uid = child.source_case_uid
         AND parent.wikidisputes_id_exact = child.wikidisputes_reply_to_exact
        WHERE child.wikidisputes_reply_to_exact IS NOT NULL
          AND parent.source_row_uid IS NULL
        """
    ).fetchone()[0]
    unresolved_alias = connection.execute(
        """
        WITH aliases AS (
          SELECT DISTINCT source_case_uid, alias
          FROM source_rows,
          UNNEST([wikidisputes_id_exact, wikidisputes_original_id_exact]) AS item(alias)
          WHERE alias IS NOT NULL
        )
        SELECT count(*)
        FROM source_rows child
        LEFT JOIN aliases target
          ON target.source_case_uid = child.source_case_uid
         AND target.alias = child.wikidisputes_reply_to_exact
        WHERE child.wikidisputes_reply_to_exact IS NOT NULL
          AND target.alias IS NULL
        """
    ).fetchone()[0]
    edge_temporality = connection.execute(
        """
        WITH candidates AS (
          SELECT child.source_row_uid,
                 child.wikidisputes_time AS child_time,
                 parent.wikidisputes_time AS parent_time
          FROM source_rows child
          JOIN source_rows parent
            ON parent.source_case_uid = child.source_case_uid
           AND (parent.wikidisputes_id_exact = child.wikidisputes_reply_to_exact
             OR parent.wikidisputes_original_id_exact = child.wikidisputes_reply_to_exact)
          QUALIFY row_number() OVER (
            PARTITION BY child.source_row_uid
            ORDER BY CASE
              WHEN parent.wikidisputes_id_exact = child.wikidisputes_reply_to_exact THEN 0
              ELSE 1
            END,
                     parent.source_row_index
          ) = 1
        )
        SELECT
          count(*) FILTER (WHERE child_time < parent_time) AS child_before_parent,
          count(*) FILTER (WHERE child_time = parent_time) AS equal_time
        FROM candidates
        """
    ).fetchone()
    tied_discussions = connection.execute(
        """
        SELECT count(DISTINCT source_case_uid)
        FROM (
          SELECT source_case_uid, wikidisputes_time
          FROM source_rows
          GROUP BY source_case_uid, wikidisputes_time
          HAVING count(*) > 1
        )
        """
    ).fetchone()[0]
    adjacent_inversions = connection.execute(
        """
        SELECT count(*)
        FROM (
          SELECT wikidisputes_time,
                 lag(wikidisputes_time) OVER (
                   PARTITION BY source_case_uid ORDER BY source_row_index
                 ) AS previous_time
          FROM source_rows
        )
        WHERE previous_time IS NOT NULL AND wikidisputes_time < previous_time
        """
    ).fetchone()[0]
    cross_label = connection.execute(
        """
        SELECT wikidisputes_conv_id_exact, list_sort(list(DISTINCT source_side)) AS sides,
               count(*) AS row_occurrences
        FROM source_rows
        WHERE wikidisputes_conv_id_exact IS NOT NULL
        GROUP BY wikidisputes_conv_id_exact
        HAVING count(DISTINCT source_side) > 1
        ORDER BY wikidisputes_conv_id_exact
        """
    ).fetchall()
    exact_duplicates = connection.execute(
        """
        SELECT source_record_sha256, count(*) AS occurrences
        FROM source_rows GROUP BY source_record_sha256 HAVING count(*) > 1
        ORDER BY occurrences DESC, source_record_sha256
        """
    ).fetchall()
    normalized_duplicates = connection.execute(
        """
        SELECT lower(trim(wikidisputes_text_exact)) AS normalized_text, count(*) AS occurrences
        FROM source_rows
        WHERE wikidisputes_text_exact IS NOT NULL
        GROUP BY normalized_text HAVING count(*) > 1
        ORDER BY occurrences DESC, normalized_text
        LIMIT 1000
        """
    ).fetchall()
    near_duplicates = _near_duplicates(
        connection.execute(
            "SELECT source_row_uid, wikidisputes_text_exact FROM source_rows "
            "ORDER BY source_row_uid"
        ).fetchall()
    )
    observed_cross_ids = {row[0] for row in cross_label}
    fixture_status = {
        fixture: {
            "observed_cross_label": fixture in observed_cross_ids,
            "analytic_status": "quarantined_pending_episode_evidence",
        }
        for fixture in CROSS_LABEL_DISCUSSION_IDS
    }
    observed = {
        "discussions": {
            "escalated": discussion_counts.get("escalated", 0),
            "non_escalated": discussion_counts.get("non_escalated", 0),
            "total": sum(discussion_counts.values()),
        },
        "rows": {
            "escalated": by_side.get("escalated", 0),
            "non_escalated": by_side.get("non_escalated", 0),
            "total": total_rows,
        },
        "types": type_counts,
    }
    report = {
        "baseline": {
            "observed": observed,
            "expected": EXPECTED_COUNTS,
            "pass": observed == EXPECTED_COUNTS,
        },
        "known_fixtures": {
            "unique_current_ids": unique_ids,
            "repeated_id_rows": total_rows - unique_ids,
            "raw_dangling_reply_to": raw_dangling,
            "unresolved_after_simple_original_id_alias": unresolved_alias,
            "resolvable_child_before_parent": edge_temporality[0],
            "equal_time_reply_edges": edge_temporality[1],
            "discussions_with_timestamp_tie": tied_discussions,
            "simple_adjacent_timestamp_inversions": adjacent_inversions,
        },
        "cross_label": {
            "conversation_count": len(cross_label),
            "rows": [
                {"conversation_id": row[0], "sides": row[1], "row_occurrences": row[2]}
                for row in cross_label
            ],
            "mandatory_fixtures": fixture_status,
        },
        "duplicate_reports": {
            "exact_record_groups": len(exact_duplicates),
            "exact": [{"sha256": row[0], "occurrences": row[1]} for row in exact_duplicates],
            "normalized_top_1000": [
                {
                    "normalized_text_sha256": sha256_bytes(row[0].encode("utf-8")),
                    "normalized_character_length": len(row[0]),
                    "normalized_text_preview": row[0][:160],
                    "occurrences": row[1],
                }
                for row in normalized_duplicates
            ],
            "near_duplicate_method": (
                "word-trigram simhash64, four 16-bit candidate bands, Hamming distance <=3; "
                "diagnostic candidates only"
            ),
            "near_duplicate_candidates_top_1000": near_duplicates,
            "canonical_deduplication_policy": "identity_evidence_only_never_text_equality",
        },
    }
    atomic_write_json(report_root / "source_audit.json", report)
    atomic_write_json(report_root / "cross_label_fixtures.json", report["cross_label"])
    atomic_write_json(report_root / "duplicate_audit.json", report["duplicate_reports"])
    if not report["baseline"]["pass"]:
        raise RuntimeError("source baseline audit failed; downstream production blocked")
    return report
