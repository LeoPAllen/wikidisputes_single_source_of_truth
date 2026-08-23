from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from wikidisputes_ssot.full import _uid


ROOT = Path(__file__).resolve().parents[1]

SILVER = ROOT / "output" / "silver"

RECOVERY = SILVER / "mediawiki_raw_comment_recovery.parquet"
ACTIONS = SILVER / "utterance_actions.parquet"

OUTPUT = (
    SILVER
    / "mediawiki_raw_comment_representations.parquet"
)

REPORT = (
    ROOT
    / "reports"
    / "mediawiki_raw_comment_promotion_report.json"
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def main() -> None:
    if not RECOVERY.exists():
        raise FileNotFoundError(RECOVERY)

    if not ACTIONS.exists():
        raise FileNotFoundError(ACTIONS)

    con = duckdb.connect()

    recovery_cur = con.execute(
        f"""
        SELECT *
        FROM read_parquet(
            '{str(RECOVERY).replace("'", "''")}'
        )
        WHERE recovery_status = 'high_confidence'
        ORDER BY source_row_uid
        """
    )

    recovery_names = [
        x[0]
        for x in recovery_cur.description
    ]

    recoveries = [
        dict(zip(recovery_names, row))
        for row in recovery_cur.fetchall()
    ]

    action_cur = con.execute(
        f"""
        SELECT
            action_uid,
            version_uid,
            logical_utterance_uid,
            source_row_uid,
            source_row_uids_json,
            action_id_exact,
            raw_timestamp,
            revision_id
        FROM read_parquet(
            '{str(ACTIONS).replace("'", "''")}'
        )
        """
    )

    action_names = [
        x[0]
        for x in action_cur.description
    ]

    actions = [
        dict(zip(action_names, row))
        for row in action_cur.fetchall()
    ]

    by_action: dict[
        tuple[str, str],
        list[dict[str, Any]]
    ] = defaultdict(list)

    for action in actions:
        logical = action.get(
            "logical_utterance_uid"
        )
        action_id = action.get(
            "action_id_exact"
        )

        if logical is None or action_id is None:
            continue

        by_action[
            (
                str(logical),
                str(action_id),
            )
        ].append(action)

    representation_rows = []

    failures = []

    for rec in recoveries:
        source_uid = str(
            rec["source_row_uid"]
        )

        logical_uid = str(
            rec["logical_utterance_uid"]
        )

        utterance_id = str(
            rec["utterance_id"]
        )

        candidates = list(
            by_action.get(
                (
                    logical_uid,
                    utterance_id,
                ),
                [],
            )
        )

        # Prefer the action explicitly associated with
        # this exact WikiDisputes source occurrence.
        exact = [
            action
            for action in candidates
            if (
                action.get("source_row_uid")
                is not None
                and str(
                    action["source_row_uid"]
                )
                == source_uid
            )
        ]

        if len(exact) == 1:
            action = exact[0]

        else:
            contains_source = []

            for candidate in candidates:
                raw = candidate.get(
                    "source_row_uids_json"
                )

                try:
                    source_uids = (
                        json.loads(raw)
                        if isinstance(raw, str)
                        else []
                    )
                except json.JSONDecodeError:
                    source_uids = []

                if source_uid in {
                    str(x)
                    for x in source_uids
                }:
                    contains_source.append(
                        candidate
                    )

            if len(contains_source) == 1:
                action = contains_source[0]

            elif len(candidates) == 1:
                action = candidates[0]

            else:
                failures.append(
                    {
                        "source_row_uid":
                            source_uid,
                        "logical_utterance_uid":
                            logical_uid,
                        "utterance_id":
                            utterance_id,
                        "candidate_actions":
                            len(candidates),
                    }
                )
                continue

        version_uid = str(
            action["version_uid"]
        )

        raw_text = rec.get(
            "recovered_raw_wikitext"
        )

        body_text = rec.get(
            "recovered_body_wikitext"
        )

        if (
            not isinstance(raw_text, str)
            or not raw_text.strip()
            or not isinstance(body_text, str)
            or not body_text.strip()
        ):
            failures.append(
                {
                    "source_row_uid":
                        source_uid,
                    "logical_utterance_uid":
                        logical_uid,
                    "utterance_id":
                        utterance_id,
                    "error":
                        "blank_recovered_text",
                }
            )
            continue

        common = {
            "logical_utterance_uid":
                logical_uid,
            "version_uid":
                version_uid,
            "source_row_uid":
                source_uid,
            "source_revision_id":
                str(rec["revision_id"]),
            "revision_sha1":
                rec.get("revision_sha1"),
            "revision_timestamp":
                rec.get(
                    "revision_timestamp"
                ),
            "extraction_method":
                (
                    "mediawiki_revision_comment_"
                    "segmentation_normalized_match"
                ),
            "extraction_version":
                "1.0.0",
            "availability_status":
                "recovered",
            "leakage_class":
                "source_available",
            "confidence":
                "high_confidence_comment_match",
            "best_similarity":
                rec.get("best_similarity"),
            "second_similarity":
                rec.get(
                    "second_similarity"
                ),
            "match_margin":
                rec.get("match_margin"),
            "offset_distance":
                rec.get(
                    "offset_distance"
                ),
            "utterance_id":
                utterance_id,
        }

        specs = [
            (
                "mediawiki_revision_comment_wikitext_raw",
                raw_text,
                (
                    "archival_full_comment_"
                    "including_signature"
                ),
            ),
            (
                "mediawiki_revision_comment_wikitext_body",
                body_text,
                (
                    "annotation_body_"
                    "signature_removed"
                ),
            ),
        ]

        for kind, content, scope in specs:
            encoded = content.encode(
                "utf-8"
            )

            representation_rows.append(
                {
                    "representation_uid":
                        _uid(
                            "wdrepr",
                            version_uid,
                            kind,
                            source_uid,
                            "mediawiki_raw_v1",
                        ),
                    **common,
                    "representation_kind":
                        kind,
                    "representation_scope":
                        scope,
                    "content_sha256":
                        sha256_text(content),
                    "byte_length":
                        len(encoded),
                    "encoding":
                        "utf-8",
                    "mime_type":
                        "text/x-wiki",
                    "content_inline":
                        content,
                }
            )

    if failures:
        failure_path = (
            ROOT
            / "reports"
            / "mediawiki_raw_comment_promotion_failures.json"
        )

        failure_path.write_text(
            json.dumps(
                failures,
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        raise RuntimeError(
            f"{len(failures):,} high-confidence "
            "recoveries could not be mapped exactly "
            f"to an SSOT action. See {failure_path}"
        )

    expected = 2 * len(recoveries)

    if len(representation_rows) != expected:
        raise RuntimeError(
            "Representation count mismatch: "
            f"{len(representation_rows):,} != "
            f"{expected:,}"
        )

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    table = pa.Table.from_pylist(
        representation_rows
    )

    tmp = OUTPUT.with_suffix(
        ".parquet.tmp"
    )

    pq.write_table(
        table,
        tmp,
        compression="zstd",
    )

    tmp.replace(OUTPUT)

    report = {
        "status": "pass",
        "high_confidence_source_occurrences":
            len(recoveries),
        "representations_written":
            len(representation_rows),
        "raw_representations":
            len(recoveries),
        "body_representations":
            len(recoveries),
        "output":
            str(OUTPUT),
        "annotation_representation_kind":
            "mediawiki_revision_comment_wikitext_body",
        "archival_representation_kind":
            "mediawiki_revision_comment_wikitext_raw",
    }

    REPORT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    REPORT.write_text(
        json.dumps(
            report,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            report,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
