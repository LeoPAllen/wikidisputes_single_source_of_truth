from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from .constants import CROSS_LABEL_DISCUSSION_IDS
from .hashing import canonical_json_hash
from .io import atomic_parquet, atomic_write_json, file_descriptor, table_from_union_pylist


def _sample(
    rows: list[dict[str, Any]], id_field: str, seed: int, maximum: int = 5
) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: canonical_json_hash([seed, str(row.get(id_field))]),
    )[:maximum]


def materialize_review_packet(output_root: Path, seed: int) -> dict[str, Any]:
    packet: list[dict[str, Any]] = []
    populations: Counter[str] = Counter()

    def add_rows(
        source: str,
        rows: list[dict[str, Any]],
        id_field: str,
        stratum_field: str,
        evidence_fields: tuple[str, ...],
    ) -> None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get(stratum_field))].append(row)
        for stratum, members in grouped.items():
            label = f"{source}:{stratum}"
            populations[label] = len(members)
            for row in _sample(members, id_field, seed):
                evidence = {field: row.get(field) for field in evidence_fields}
                packet.append(
                    {
                        "review_row_uid": "wdreview:v1:"
                        + canonical_json_hash([label, row.get(id_field)]),
                        "review_source": source,
                        "stratum": stratum,
                        "entity_uid": row.get(id_field),
                        "evidence_json": str(evidence),
                        "population_count": len(members),
                        "sample_seed": seed,
                        "adjudication": None,
                        "adjudicator": None,
                        "adjudicated_at": None,
                        "notes": None,
                        "manual_validation_status": "not_reviewed",
                    }
                )

    outcomes_path = output_root / "silver" / "outcomes.parquet"
    if outcomes_path.exists():
        outcomes = pq.read_table(outcomes_path).to_pylist()
        for row in outcomes:
            row["review_uid"] = canonical_json_hash(
                [row["episode_uid"], row["definition_id"], row["horizon_days"]]
            )
            row["review_stratum"] = (
                f"{row['definition_id']}:{row['observation_status']}:{row['applicability_status']}"
            )
        add_rows(
            "dv",
            outcomes,
            "review_uid",
            "review_stratum",
            (
                "episode_uid",
                "definition_id",
                "horizon_days",
                "observed_value_json",
                "event_time_utc",
                "evidence_uids_json",
                "censoring_reason",
            ),
        )

    reply_path = output_root / "silver" / "reply_edges.parquet"
    if reply_path.exists():
        add_rows(
            "reply",
            pq.read_table(reply_path).to_pylist(),
            "reply_edge_uid",
            "resolution_status",
            (
                "source_logical_utterance_uid",
                "raw_reply_target",
                "target_logical_utterance_uid",
                "resolution_method",
                "error_reason",
            ),
        )

    signature_path = output_root / "silver" / "signatures.parquet"
    if signature_path.exists():
        add_rows(
            "signature",
            pq.read_table(signature_path).to_pylist(),
            "signature_uid",
            "signature_status",
            (
                "logical_utterance_uid",
                "raw_signature_wikitext",
                "user_target",
                "user_talk_target",
                "contributions_target",
                "actor_match_status",
            ),
        )

    identity_path = output_root / "silver" / "identity_registry.parquet"
    if identity_path.exists():
        add_rows(
            "identity",
            pq.read_table(identity_path).to_pylist(),
            "registry_entry_uid",
            "adjudication_status",
            (
                "issued_uid",
                "derivation_method",
                "selected_anchor",
                "candidate_anchors_json",
                "confidence",
            ),
        )

    quality_path = output_root / "silver" / "quality_flags.parquet"
    if quality_path.exists():
        quality_rows = pq.read_table(quality_path).to_pylist()
        if quality_rows and "quality_flag_uid" in quality_rows[0]:
            add_rows(
                "quality_candidate",
                quality_rows,
                "quality_flag_uid",
                "flag_code",
                ("entity_uid", "severity", "evidence_pointer"),
            )

    revision_path = output_root / "silver" / "talk_page_revision_observations.parquet"
    if revision_path.exists():
        add_rows(
            "historical_text_availability",
            pq.read_table(revision_path).to_pylist(),
            "revision_observation_uid",
            "availability_status",
            (
                "revision_id",
                "page_id",
                "title_at_retrieval",
                "texthidden",
                "userhidden",
                "response_blob_path",
            ),
        )

    discrepancy_path = output_root / "reports" / "representation_discrepancies.parquet"
    if discrepancy_path.exists():
        discrepancy_rows = pq.read_table(discrepancy_path).to_pylist()
        if discrepancy_rows and "discrepancy_uid" in discrepancy_rows[0]:
            add_rows(
                "representation_discrepancy",
                discrepancy_rows,
                "discrepancy_uid",
                "category",
                (
                    "logical_utterance_uid",
                    "version_uid",
                    "source_visible_sha256",
                    "reconstructed_visible_sha256",
                    "evidence_pointer",
                ),
            )

    links_path = output_root / "silver" / "links.parquet"
    if links_path.exists():
        link_rows = pq.read_table(links_path).to_pylist()
        if link_rows and "link_uid" in link_rows[0]:
            add_rows(
                "link",
                link_rows,
                "link_uid",
                "link_kind",
                (
                    "logical_utterance_uid",
                    "raw_link_wikitext",
                    "raw_target",
                    "normalized_target",
                    "recovered_from_revision",
                    "evidence_pointer",
                ),
            )

    episodes = pq.read_table(output_root / "silver" / "dispute_episodes.parquet").to_pylist()
    disputes = {
        str(row["dispute_uid"]): row
        for row in pq.read_table(output_root / "silver" / "disputes.parquet").to_pylist()
    }
    episode_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in pq.read_table(output_root / "silver" / "events.parquet").to_pylist():
        if event.get("episode_uid"):
            episode_events[str(event["episode_uid"])].append(event)
    cross_label = [
        row
        for row in episodes
        if row.get("source_conversation_id_exact") in CROSS_LABEL_DISCUSSION_IDS
    ]
    for row in cross_label:
        row["review_stratum"] = "cross_label"
        dispute = disputes.get(str(row["dispute_uid"]), {})
        row["source_dispute_json_canonical"] = dispute.get("dispute_json_canonical")
        row["event_evidence_json"] = str(episode_events.get(str(row["episode_uid"]), []))
    add_rows(
        "episode",
        cross_label,
        "episode_uid",
        "review_stratum",
        (
            "dispute_uid",
            "source_conversation_id_exact",
            "source_wikidisputes_escalated",
            "alignment_status",
            "analysis_status",
            "alignment_reasons",
            "title_at_event_exact",
            "page_id_exact",
            "thread_start_at",
            "source_projection_end_at",
            "source_dispute_json_canonical",
            "event_evidence_json",
        ),
    )

    target = output_root / "manual_review" / "ssot_review_packet.parquet"
    atomic_parquet(target, table_from_union_pylist(packet))
    manifest = {
        "review_packet_version": "1.0.0",
        "seed": seed,
        "population_counts": dict(sorted(populations.items())),
        "sample_rows": len(packet),
        "artifact": {**file_descriptor(target), "rows": len(packet)},
        "human_validation_status": "not_reviewed",
        "definition_status": "candidate",
        "instructions": "docs/MANUAL_REVIEW.md",
    }
    atomic_write_json(output_root / "reports" / "manual_review_packet.json", manifest)
    return manifest
