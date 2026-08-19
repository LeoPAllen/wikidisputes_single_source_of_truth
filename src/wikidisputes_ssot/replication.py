from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .hashing import canonical_json_hash
from .io import atomic_parquet, atomic_write_json, file_descriptor

WORD = re.compile(r"\S+")


def materialize_replication_views(output_root: Path) -> dict[str, Any]:
    source = pq.read_table(
        output_root / "canonical" / "wikidisputes_source_projection.parquet"
    ).to_pylist()
    ids_by_case: dict[str, set[str]] = defaultdict(set)
    aliases_by_case: dict[str, set[str]] = defaultdict(set)
    current_counts_by_case: Counter[tuple[str, str]] = Counter()
    for row in source:
        case = str(row["source_case_uid"])
        current = row.get("wikidisputes_id_exact")
        original = row.get("wikidisputes_original_id_exact")
        if current:
            ids_by_case[case].add(str(current))
            aliases_by_case[case].add(str(current))
            current_counts_by_case[(case, str(current))] += 1
        if original:
            aliases_by_case[case].add(str(original))

    flags: list[dict[str, Any]] = []
    problems_by_case: dict[str, set[str]] = defaultdict(set)
    merge_key_occurrence: Counter[tuple[str, str, str]] = Counter()
    for row in source:
        case = str(row["source_case_uid"])
        text = row.get("wikidisputes_text_exact") or ""
        word_count = len(WORD.findall(text))
        current = row.get("wikidisputes_id_exact")
        target = row.get("wikidisputes_reply_to_exact")
        exact_duplicate_id = bool(current and current_counts_by_case[(case, str(current))] > 1)
        dangling_raw = bool(target and str(target) not in ids_by_case[case])
        dangling_after_original_alias = bool(target and str(target) not in aliases_by_case[case])
        if word_count > 1000:
            problems_by_case[case].add("over_1000_whitespace_tokens")
        if exact_duplicate_id:
            problems_by_case[case].add("duplicate_current_id_within_discussion")
        if dangling_raw:
            problems_by_case[case].add("raw_dangling_parent")
        author = str(row.get("wikidisputes_user_exact") or "")
        addressee = str(target or "")
        merge_key_occurrence[(case, author, addressee)] += 1
        flags.append(
            {
                "source_row_uid": row["source_row_uid"],
                "source_case_uid": case,
                "source_side": row["source_side"],
                "source_order": row["source_order"],
                "word_count_whitespace_v1": word_count,
                "over_1000_words_whitespace_v1": word_count > 1000,
                "duplicate_current_id_within_discussion": exact_duplicate_id,
                "raw_dangling_parent": dangling_raw,
                "dangling_after_original_id_alias": dangling_after_original_alias,
                "same_author_same_raw_addressee_group_uid": "wdrepmerge:v1:"
                + canonical_json_hash([case, author, addressee]),
                "canonical_row_retained": True,
                "replication_version": "vasilets-2024-replication-v1",
            }
        )

    for row in flags:
        case_problems = problems_by_case[row["source_case_uid"]]
        row["discussion_has_over_1000_words_whitespace_v1"] = (
            "over_1000_whitespace_tokens" in case_problems
        )
        row["discussion_has_duplicate_or_dangling"] = bool(
            case_problems & {"duplicate_current_id_within_discussion", "raw_dangling_parent"}
        )
        row["eligible_after_length_whitespace_v1"] = not row[
            "discussion_has_over_1000_words_whitespace_v1"
        ]
        row["eligible_after_duplicate_dangling_v1"] = (
            row["eligible_after_length_whitespace_v1"]
            and not row["duplicate_current_id_within_discussion"]
            and not row["raw_dangling_parent"]
        )
        row["complete_discussion_v1"] = not case_problems

    output = output_root / "analysis" / "replication_vasilets_2024_sequence.parquet"
    atomic_parquet(output, pa.Table.from_pylist(flags))
    flag_by_uid = {str(row["source_row_uid"]): row for row in flags}
    merge_runs: list[dict[str, Any]] = []
    rows_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        if flag_by_uid[str(row["source_row_uid"])]["complete_discussion_v1"]:
            rows_by_case[str(row["source_case_uid"])].append(row)
    for case_uid, rows in rows_by_case.items():
        rows.sort(key=lambda row: int(row["source_row_index"]))
        current_run: list[dict[str, Any]] = []
        current_key: tuple[str, str] | None = None
        for row in rows:
            key = (
                str(row.get("wikidisputes_user_exact") or ""),
                str(row.get("wikidisputes_reply_to_exact") or ""),
            )
            if current_run and key != current_key:
                source_uids = [str(item["source_row_uid"]) for item in current_run]
                merge_runs.append(
                    {
                        "replication_run_uid": "wdrepmerge-run:v1:"
                        + canonical_json_hash([case_uid, source_uids]),
                        "source_case_uid": case_uid,
                        "source_side": current_run[0]["source_side"],
                        "wikidisputes_user_exact": current_key[0] if current_key else None,
                        "raw_addressee_exact": current_key[1] if current_key else None,
                        "source_row_uids_json": json.dumps(source_uids),
                        "source_row_count": len(current_run),
                        "merged_text_derivative": "\n".join(
                            str(item.get("wikidisputes_text_exact") or "") for item in current_run
                        ),
                        "canonical_mutation": False,
                        "replication_version": "vasilets-2024-replication-v1",
                    }
                )
                current_run = []
            current_key = key
            current_run.append(row)
        if current_run:
            source_uids = [str(item["source_row_uid"]) for item in current_run]
            merge_runs.append(
                {
                    "replication_run_uid": "wdrepmerge-run:v1:"
                    + canonical_json_hash([case_uid, source_uids]),
                    "source_case_uid": case_uid,
                    "source_side": current_run[0]["source_side"],
                    "wikidisputes_user_exact": current_key[0] if current_key else None,
                    "raw_addressee_exact": current_key[1] if current_key else None,
                    "source_row_uids_json": json.dumps(source_uids),
                    "source_row_count": len(current_run),
                    "merged_text_derivative": "\n".join(
                        str(item.get("wikidisputes_text_exact") or "") for item in current_run
                    ),
                    "canonical_mutation": False,
                    "replication_version": "vasilets-2024-replication-v1",
                }
            )
    merge_output = (
        output_root / "analysis" / "replication_vasilets_2024_consecutive_merge_candidates.parquet"
    )
    atomic_parquet(merge_output, pa.Table.from_pylist(merge_runs))
    counts: dict[str, Any] = {}
    for side in ("escalated", "non_escalated"):
        subset = [row for row in flags if row["source_side"] == side]
        counts[side] = {
            "initial_rows": len(subset),
            "initial_discussions": len({row["source_case_uid"] for row in subset}),
            "after_length_rows": sum(row["eligible_after_length_whitespace_v1"] for row in subset),
            "after_length_discussions": len(
                {
                    row["source_case_uid"]
                    for row in subset
                    if row["eligible_after_length_whitespace_v1"]
                }
            ),
            "after_duplicate_dangling_rows": sum(
                row["eligible_after_duplicate_dangling_v1"] for row in subset
            ),
            "complete_rows": sum(row["complete_discussion_v1"] for row in subset),
        }
    report = {
        "artifact": {**file_descriptor(output), "rows": len(flags)},
        "consecutive_merge_artifact": {
            **file_descriptor(merge_output),
            "rows": len(merge_runs),
            "multirow_runs": sum(row["source_row_count"] > 1 for row in merge_runs),
        },
        "counts": counts,
        "canonical_mutation": False,
        "published_count_match_status": (
            "partial: whitespace-token and documented structural approximation; "
            "paper implementation/tokenizer and manual disagreement filter unavailable"
        ),
        "merge_policy": (
            "explicit consecutive raw-author/raw-addressee candidate-run view emitted; "
            "canonical turns are never merged and no annotation is propagated"
        ),
    }
    atomic_write_json(output_root / "reports" / "literature_replication.json", report)
    return report
