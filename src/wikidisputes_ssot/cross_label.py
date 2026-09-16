from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from .constants import CROSS_LABEL_DISCUSSION_IDS
from .io import atomic_write_json


def _read_matching_rows(
    path: Path,
    *,
    match_column: str,
    accepted_values: set[str],
    columns: list[str] | None = None,
    batch_size: int = 10_000,
) -> list[dict[str, Any]]:
    """Batch-scan projected Parquet data, retaining only requested identities."""
    if not accepted_values:
        return []
    requested_columns = list(columns) if columns is not None else None
    if requested_columns is not None and match_column not in requested_columns:
        requested_columns.append(match_column)
    matches: list[dict[str, Any]] = []
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=batch_size, columns=requested_columns):
        matches.extend(
            row for row in batch.to_pylist() if str(row.get(match_column)) in accepted_values
        )
    return matches


def resolve_cross_label_policy(
    *, same_episode: bool | None, formal_escalation_verified: bool
) -> dict[str, Any]:
    """Apply the binding cross-label policy without inferring missing evidence."""
    if same_episode is True and formal_escalation_verified:
        return {
            "analytic_status": "positive_formal_event_with_contradictory_source_provenance",
            "analytic_outcome": True,
        }
    if same_episode is False:
        return {
            "analytic_status": "distinct_non_overlapping_episodes",
            "analytic_outcome": None,
        }
    return {
        "analytic_status": "quarantined_episode_identity_unresolved",
        "analytic_outcome": None,
    }


def materialize_cross_label_reconciliation(output_root: Path) -> dict[str, Any]:
    conversation_ids = set(CROSS_LABEL_DISCUSSION_IDS)
    projection = _read_matching_rows(
        output_root / "canonical" / "wikidisputes_source_projection.parquet",
        match_column="wikidisputes_conv_id_exact",
        accepted_values=conversation_ids,
        columns=[
            "wikidisputes_conv_id_exact",
            "source_side",
            "source_repository",
            "source_commit",
            "archive_sha256",
            "archive_member_path",
            "source_case_index",
        ],
    )
    episodes = _read_matching_rows(
        output_root / "silver" / "dispute_episodes.parquet",
        match_column="source_conversation_id_exact",
        accepted_values=conversation_ids,
        columns=[
            "episode_uid",
            "source_conversation_id_exact",
            "source_wikidisputes_escalated",
            "analysis_status",
            "thread_start_at",
            "source_projection_end_at",
            "title_at_event_exact",
            "page_id_exact",
            "alignment_status",
        ],
    )
    episode_uids = {str(row["episode_uid"]) for row in episodes}
    threads = _read_matching_rows(
        output_root / "silver" / "episode_threads.parquet",
        match_column="episode_uid",
        accepted_values=episode_uids,
    )
    events = _read_matching_rows(
        output_root / "silver" / "events.parquet",
        match_column="episode_uid",
        accepted_values=episode_uids,
        columns=[
            "episode_uid",
            "event_uid",
            "event_type",
            "event_subtype",
            "event_time_exact",
            "source_url_exact",
            "tag_name_exact",
            "title_at_event_exact",
        ],
    )
    utterances = {
        str(row["logical_utterance_uid"]): row
        for row in _read_matching_rows(
            output_root / "silver" / "utterances.parquet",
            match_column="conversation_id_exact",
            accepted_values=conversation_ids,
            columns=["logical_utterance_uid", "conversation_id_exact"],
        )
    }
    actions = _read_matching_rows(
        output_root / "silver" / "utterance_actions.parquet",
        match_column="logical_utterance_uid",
        accepted_values=set(utterances),
        columns=["logical_utterance_uid", "action_type"],
    )
    episodes_by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        episodes_by_conversation[str(episode["source_conversation_id_exact"])].append(episode)
    threads_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for thread in threads:
        threads_by_episode[str(thread["episode_uid"])].append(thread)
    events_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("episode_uid"):
            events_by_episode[str(event["episode_uid"])].append(event)
    lifecycle_by_conversation: dict[str, Counter[str]] = defaultdict(Counter)
    for action in actions:
        utterance = utterances.get(str(action["logical_utterance_uid"]))
        if utterance:
            lifecycle_by_conversation[str(utterance["conversation_id_exact"])][
                str(action["action_type"])
            ] += 1
    fixtures: dict[str, Any] = {}
    for conversation_id in CROSS_LABEL_DISCUSSION_IDS:
        source_rows = [
            row for row in projection if row["wikidisputes_conv_id_exact"] == conversation_id
        ]
        fixture_episodes = episodes_by_conversation.get(conversation_id, [])
        episode_evidence = []
        for episode in fixture_episodes:
            episode_uid = str(episode["episode_uid"])
            episode_evidence.append(
                {
                    "episode_uid": episode_uid,
                    "source_wikidisputes_escalated": episode["source_wikidisputes_escalated"],
                    "analysis_status": episode.get("analysis_status"),
                    "thread_start_at": episode.get("thread_start_at"),
                    "source_projection_end_at": episode.get("source_projection_end_at"),
                    "title_at_event_exact": episode.get("title_at_event_exact"),
                    "page_id_exact": episode.get("page_id_exact"),
                    "alignment_status": episode.get("alignment_status"),
                    "threads": threads_by_episode.get(episode_uid, []),
                    "events": [
                        {
                            key: event.get(key)
                            for key in (
                                "event_uid",
                                "event_type",
                                "event_subtype",
                                "event_time_exact",
                                "source_url_exact",
                                "tag_name_exact",
                                "title_at_event_exact",
                            )
                        }
                        for event in events_by_episode.get(episode_uid, [])
                    ],
                }
            )
        policy = resolve_cross_label_policy(
            same_episode=None,
            formal_escalation_verified=any(
                event.get("event_type") == "formal_process"
                for episode in fixture_episodes
                for event in events_by_episode.get(str(episode["episode_uid"]), [])
            ),
        )
        fixtures[conversation_id] = {
            **policy,
            "source_sides": sorted({str(row["source_side"]) for row in source_rows}),
            "source_row_count": len(source_rows),
            "source_versions": sorted(
                {
                    json.dumps(
                        {
                            "repository": row["source_repository"],
                            "commit": row["source_commit"],
                            "archive_sha256": row["archive_sha256"],
                            "file": row["archive_member_path"],
                            "case_index": row["source_case_index"],
                        },
                        sort_keys=True,
                    )
                    for row in source_rows
                }
            ),
            "lifecycle_counts": dict(lifecycle_by_conversation.get(conversation_id, {})),
            "episodes": episode_evidence,
            "resolution_reason": (
                "tag and DRN source records share a WikiConv conversation ID, but the "
                "available evidence does not yet prove one episode or non-overlapping episodes"
            ),
        }
    report = {
        "fixture_count": len(fixtures),
        "all_source_sides_preserved": all(
            value["source_sides"] == ["escalated", "non_escalated"] for value in fixtures.values()
        ),
        "contradictory_analytic_outcome_count": 0,
        "fixtures": fixtures,
    }
    atomic_write_json(output_root / "reports" / "cross_label_episode_reconciliation.json", report)
    return report
