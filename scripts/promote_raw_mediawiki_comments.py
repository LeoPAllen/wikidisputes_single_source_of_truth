from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from wikidisputes_ssot.full import _uid
from wikidisputes_ssot.promotion_safety import SafetyDecision, assess_promotion
from wikidisputes_ssot.source_provenance import check_source_text_provenance

ROOT = Path(__file__).resolve().parents[1]
SILVER = ROOT / "output" / "silver"
DEFAULT_RECOVERY = SILVER / "mediawiki_raw_comment_recovery.parquet"
DEFAULT_ACTIONS = SILVER / "utterance_actions.parquet"
DEFAULT_OUTPUT = SILVER / "mediawiki_raw_comment_representations.parquet"
DEFAULT_REPORT = ROOT / "reports" / "mediawiki_raw_comment_promotion_report.json"
DEFAULT_AUDIT = ROOT / "reports" / "mediawiki_raw_comment_promotion_audit.parquet"
DEFAULT_TRUSTED = (
    ROOT / "output" / "annotation" / "wikidisputes_llm_annotation_input.pre_raw_wikitext.csv"
)
DEFAULT_CANONICAL = (
    ROOT / "output" / "canonical" / "wikidisputes_annotation_join_contract.parquet"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply the conservative promotion-safety gate to MediaWiki candidates."
    )
    parser.add_argument("--recovery", type=Path, default=DEFAULT_RECOVERY)
    parser.add_argument("--actions", type=Path, default=DEFAULT_ACTIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument(
        "--trusted-annotation",
        type=Path,
        default=DEFAULT_TRUSTED,
        help=(
            "Pre-recovery annotation CSV keyed by ssot_source_row_uid. "
            "If absent, recovery-row source evidence is used conservatively."
        ),
    )
    parser.add_argument(
        "--canonical-source",
        type=Path,
        default=DEFAULT_CANONICAL,
        help="Canonical join contract used to verify immutable source targets.",
    )
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def qpath(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def rows(con: duckdb.DuckDBPyConnection, query: str) -> list[dict[str, Any]]:
    cur = con.execute(query)
    names = [column[0] for column in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def load_trusted_texts(
    con: duckdb.DuckDBPyConnection, path: Path
) -> tuple[dict[str, str], dict[str, list[str]]]:
    if not path.exists():
        return {}, {}
    records = rows(
        con,
        f"""
        SELECT
            CAST(ssot_source_row_uid AS VARCHAR) AS source_row_uid,
            CAST(ssot_episode_uid AS VARCHAR) AS episode_uid,
            TRY_CAST(utterance_order AS BIGINT) AS utterance_order,
            CAST(ssot_source_text_exact AS VARCHAR) AS trusted_text
        FROM read_csv_auto(
            '{qpath(path)}', HEADER=TRUE, ALL_VARCHAR=TRUE,
            SAMPLE_SIZE=-1, MAX_LINE_SIZE=100000000
        )
        WHERE utterance_role = 'utterance'
        """,
    )
    trusted = {str(row["source_row_uid"]): str(row.get("trusted_text") or "") for row in records}
    by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        by_episode[str(row["episode_uid"])].append(row)
    neighbors: dict[str, list[str]] = defaultdict(list)
    for episode_rows in by_episode.values():
        episode_rows.sort(
            key=lambda row: (row.get("utterance_order") is None, row.get("utterance_order"))
        )
        for index, row in enumerate(episode_rows):
            uid = str(row["source_row_uid"])
            for adjacent_index in (index - 1, index + 1):
                if 0 <= adjacent_index < len(episode_rows):
                    neighbors[uid].append(
                        str(episode_rows[adjacent_index].get("trusted_text") or "")
                    )
    return trusted, dict(neighbors)


def recovery_fallback(rec: dict[str, Any]) -> str:
    current = str(rec.get("current_annotation_text") or "")
    current_source = str(rec.get("current_annotation_text_source") or "")
    if current.strip() and not current_source.startswith("mediawiki_revision_comment_"):
        return current
    return str(rec.get("source_text") or "")


def action_for_recovery(
    rec: dict[str, Any], by_action: dict[tuple[str, str], list[dict[str, Any]]]
) -> dict[str, Any] | None:
    source_uid = str(rec["source_row_uid"])
    candidates = list(
        by_action.get((str(rec["logical_utterance_uid"]), str(rec["utterance_id"])), [])
    )
    exact = [row for row in candidates if str(row.get("source_row_uid") or "") == source_uid]
    if len(exact) == 1:
        return exact[0]
    contained = []
    for candidate in candidates:
        try:
            source_uids = json.loads(candidate.get("source_row_uids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            source_uids = []
        if source_uid in {str(value) for value in source_uids}:
            contained.append(candidate)
    if len(contained) == 1:
        return contained[0]
    return candidates[0] if len(candidates) == 1 else None


def safety_columns(decision: SafetyDecision) -> dict[str, Any]:
    data = decision.to_dict()
    for key in (
        "reasons",
        "deleted_token_spans",
        "added_token_spans",
        "missing_critical_tokens",
        "added_critical_tokens",
        "structural_flags",
        "trusted_comparison_adjustments",
    ):
        data[key] = json.dumps(data[key], ensure_ascii=False)
    return data


def main() -> None:
    args = parse_args()
    if not args.recovery.exists():
        raise FileNotFoundError(args.recovery)
    if not args.actions.exists():
        raise FileNotFoundError(args.actions)
    if not args.canonical_source.exists():
        raise FileNotFoundError(args.canonical_source)

    con = duckdb.connect()
    recoveries = rows(
        con, f"SELECT * FROM read_parquet('{qpath(args.recovery)}') ORDER BY source_row_uid"
    )
    actions = rows(
        con,
        f"""
        SELECT action_uid, version_uid, logical_utterance_uid, source_row_uid,
               source_row_uids_json, action_id_exact, raw_timestamp, revision_id
        FROM read_parquet('{qpath(args.actions)}')
        """,
    )
    trusted, neighbors = load_trusted_texts(con, args.trusted_annotation)
    canonical = {
        str(row["source_row_uid"]): row.get("wikidisputes_text_exact")
        for row in rows(
            con,
            f"""
            SELECT source_row_uid, wikidisputes_text_exact
            FROM read_parquet('{qpath(args.canonical_source)}')
            """,
        )
    }
    recovery_provenance = check_source_text_provenance(recoveries, canonical)
    recovery_provenance.require_ok(label=str(args.recovery))
    if trusted:
        trusted_provenance = check_source_text_provenance(
            [
                {"source_row_uid": uid, "source_text": trusted_text}
                for uid, trusted_text in trusted.items()
            ],
            canonical,
        )
        trusted_provenance.require_ok(label=str(args.trusted_annotation))
    else:
        trusted_provenance = None

    by_action: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        if (
            action.get("logical_utterance_uid") is not None
            and action.get("action_id_exact") is not None
        ):
            by_action[
                (str(action["logical_utterance_uid"]), str(action["action_id_exact"]))
            ].append(action)

    representations: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    mapping_failures: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    promoted_by_tier: Counter[str] = Counter()

    for rec in recoveries:
        source_uid = str(rec["source_row_uid"])
        candidate = str(rec.get("recovered_body_wikitext") or "")
        raw_text = str(rec.get("recovered_raw_wikitext") or "")
        trusted_text = trusted.get(source_uid, recovery_fallback(rec))
        decision = assess_promotion(trusted_text, candidate, rec, neighbors.get(source_uid, ()))
        counts[decision.decision] += 1
        if decision.decision == "promote":
            promoted_by_tier[str(rec.get("recovery_tier") or "existing_current")] += 1
        audits.append(
            {
                "source_row_uid": source_uid,
                "logical_utterance_uid": str(rec.get("logical_utterance_uid") or ""),
                "utterance_id": str(rec.get("utterance_id") or ""),
                "recovery_status": str(rec.get("recovery_status") or ""),
                "best_similarity": rec.get("best_similarity"),
                "second_similarity": rec.get("second_similarity"),
                "match_margin": rec.get("match_margin"),
                "target_coverage": rec.get("target_coverage"),
                "candidate_purity": rec.get("candidate_purity"),
                "recovery_tier": str(rec.get("recovery_tier") or "existing_current"),
                "candidate_provenance": str(rec.get("candidate_provenance") or ""),
                "source_comparison_mode": str(rec.get("source_comparison_mode") or "exact"),
                "source_signature_artifact_reason": str(
                    rec.get("source_signature_artifact_reason") or ""
                ),
                "trusted_text": trusted_text,
                "recovered_candidate": candidate,
                "final_text": candidate if decision.decision == "promote" else trusted_text,
                **safety_columns(decision),
            }
        )

        if not raw_text.strip() and not candidate.strip():
            continue
        action = action_for_recovery(rec, by_action)
        if action is None:
            mapping_failures.append(
                {"source_row_uid": source_uid, "utterance_id": rec.get("utterance_id")}
            )
            continue

        version_uid = str(action["version_uid"])
        common = {
            "logical_utterance_uid": str(rec["logical_utterance_uid"]),
            "version_uid": version_uid,
            "source_row_uid": source_uid,
            "source_revision_id": str(rec.get("revision_id") or ""),
            "revision_sha1": rec.get("revision_sha1"),
            "revision_timestamp": rec.get("revision_timestamp"),
            "extraction_method": "mediawiki_revision_comment_segmentation_normalized_match",
            "extraction_version": "1.1.0",
            "leakage_class": "source_available",
            "confidence": (
                "high_confidence_comment_match"
                if rec.get("recovery_status") == "high_confidence"
                else str(rec.get("recovery_status") or "candidate")
            ),
            "best_similarity": rec.get("best_similarity"),
            "second_similarity": rec.get("second_similarity"),
            "match_margin": rec.get("match_margin"),
            "offset_distance": rec.get("offset_distance"),
            "utterance_id": str(rec.get("utterance_id") or ""),
            "promotion_safety_decision": decision.decision,
            "promotion_safety_reasons_json": json.dumps(decision.reasons, ensure_ascii=False),
            "ordered_token_retention": decision.ordered_token_retention,
            "candidate_token_purity": decision.candidate_token_purity,
            "recovery_tier": str(rec.get("recovery_tier") or "existing_current"),
            "candidate_provenance": str(rec.get("candidate_provenance") or ""),
            "source_comparison_mode": str(rec.get("source_comparison_mode") or "exact"),
        }
        specs: list[tuple[str, str, str, str]] = []
        if raw_text.strip():
            specs.append(
                (
                    "mediawiki_revision_comment_wikitext_raw",
                    raw_text,
                    "archival_full_comment_including_signature",
                    "candidate_preserved",
                )
            )
        if candidate.strip():
            if decision.decision == "promote":
                specs.append(
                    (
                        "mediawiki_revision_comment_wikitext_body",
                        candidate,
                        "annotation_body_signature_removed",
                        "recovered",
                    )
                )
            else:
                specs.append(
                    (
                        "mediawiki_revision_comment_wikitext_body_candidate",
                        candidate,
                        "review_candidate_signature_removed",
                        decision.decision,
                    )
                )
        for kind, content, scope, availability in specs:
            representations.append(
                {
                    "representation_uid": _uid(
                        "wdrepr", version_uid, kind, source_uid, "mediawiki_raw_v1_1"
                    ),
                    **common,
                    "representation_kind": kind,
                    "representation_scope": scope,
                    "availability_status": availability,
                    "content_sha256": sha256_text(content),
                    "byte_length": len(content.encode("utf-8")),
                    "encoding": "utf-8",
                    "mime_type": "text/x-wiki",
                    "content_inline": content,
                }
            )

    if mapping_failures:
        raise RuntimeError(
            f"{len(mapping_failures):,} nonblank recovery candidates could not be mapped exactly; "
            f"first failures: {mapping_failures[:5]}"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    output_tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    audit_tmp = args.audit.with_suffix(args.audit.suffix + ".tmp")
    pq.write_table(pa.Table.from_pylist(representations), output_tmp, compression="zstd")
    pq.write_table(pa.Table.from_pylist(audits), audit_tmp, compression="zstd")
    output_tmp.replace(args.output)
    audit_tmp.replace(args.audit)

    rejected_hc = sum(
        1
        for row in audits
        if row["recovery_status"] == "high_confidence" and row["decision"] != "promote"
    )
    report = {
        "status": "pass",
        "recovery_rows": len(recoveries),
        "safely_promoted": counts["promote"],
        "retained_fallback": counts["fallback"],
        "requiring_review": counts["review"],
        "rejected_high_confidence_candidates": rejected_hc,
        "promoted_by_recovery_tier": dict(sorted(promoted_by_tier.items())),
        "representations_written": len(representations),
        "annotation_body_representations": sum(
            row["representation_kind"] == "mediawiki_revision_comment_wikitext_body"
            for row in representations
        ),
        "candidate_body_representations": sum(
            row["representation_kind"] == "mediawiki_revision_comment_wikitext_body_candidate"
            for row in representations
        ),
        "trusted_annotation": (
            str(args.trusted_annotation) if args.trusted_annotation.exists() else None
        ),
        "canonical_source": str(args.canonical_source),
        "canonical_source_provenance": {
            "recovery_rows_checked": recovery_provenance.checked_rows,
            "trusted_rows_checked": (
                trusted_provenance.checked_rows if trusted_provenance else 0
            ),
            "mismatches": 0,
        },
        "output": str(args.output),
        "audit": str(args.audit),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
