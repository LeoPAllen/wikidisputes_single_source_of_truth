from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .constants import (
    DV_VERSION,
    IDENTITY_VERSION,
    JOIN_CONTRACT_VERSION,
    REPRESENTATION_VERSION,
    SCHEMA_VERSION,
)
from .hashing import canonical_json_hash, sha256_file
from .io import atomic_link_or_copy, atomic_parquet, atomic_write_json, file_descriptor


def materialize_exports(
    output_root: Path, canonical_config: dict[str, Any], repository_root: Path | None = None
) -> dict[str, Any]:
    canonical = output_root / "canonical"
    silver = output_root / "silver"
    analysis = output_root / "analysis"
    canonical.mkdir(parents=True, exist_ok=True)
    analysis.mkdir(parents=True, exist_ok=True)
    copies = {
        "wikidisputes_events.parquet": silver / "events.parquet",
        "wikidisputes_outcomes.parquet": silver / "outcomes.parquet",
        "wikidisputes_dv_definitions.parquet": silver / "dv_definitions.parquet",
        "wikidisputes_annotation_join_contract.parquet": silver
        / "annotation_join_contract.parquet",
        "wikidisputes_annotation_context_join_contract.parquet": silver
        / "annotation_context_join_contract.parquet",
    }
    for name, source in copies.items():
        if source.exists():
            atomic_link_or_copy(source, canonical / name)

    actions = pq.read_table(silver / "utterance_actions.parquet").to_pylist()
    context_actions_path = silver / "context_actions.parquet"
    context_actions = (
        pq.read_table(context_actions_path).to_pylist() if context_actions_path.exists() else []
    )
    events = pq.read_table(silver / "events.parquet").to_pylist()
    timeline: list[dict[str, Any]] = []
    for action in actions:
        timeline.append(
            {
                "timeline_row_uid": action["action_uid"],
                "row_kind": "utterance_action",
                "event_or_action_type": action["action_type"],
                "episode_uid": None,
                "logical_utterance_uid": action["logical_utterance_uid"],
                "context_node_uid": None,
                "time_exact": action["raw_timestamp"],
                "time_status": "source_or_wikiconv_exact",
                "evidence_pointer": action.get("source_row_uid")
                or action.get("wikiconv_source_row_uid"),
            }
        )
    for action in context_actions:
        timeline.append(
            {
                "timeline_row_uid": action["context_action_uid"],
                "row_kind": "context_action",
                "event_or_action_type": action["action_type"],
                "episode_uid": None,
                "logical_utterance_uid": None,
                "context_node_uid": action["context_node_uid"],
                "time_exact": action["raw_timestamp"],
                "time_status": "wikiconv_exact",
                "evidence_pointer": action.get("wikiconv_source_row_uid"),
            }
        )
    for event in events:
        timeline.append(
            {
                "timeline_row_uid": event["event_uid"],
                "row_kind": "event",
                "event_or_action_type": event["event_type"],
                "episode_uid": event.get("episode_uid"),
                "logical_utterance_uid": None,
                "context_node_uid": None,
                "time_exact": event.get("event_time_utc") or event.get("event_time_exact"),
                "time_status": event.get("event_time_status"),
                "evidence_pointer": None,
            }
        )
    article_history_path = silver / "article_revision_observations.parquet"
    if article_history_path.exists():
        for revision in pq.read_table(article_history_path).to_pylist():
            timeline.append(
                {
                    "timeline_row_uid": revision["article_revision_observation_uid"],
                    "row_kind": "event",
                    "event_or_action_type": "article_edit",
                    "episode_uid": None,
                    "logical_utterance_uid": None,
                    "context_node_uid": None,
                    "time_exact": revision.get("timestamp"),
                    "time_status": "mediawiki_revision_timestamp",
                    "evidence_pointer": (
                        "article_revision_observation:"
                        + revision["article_revision_observation_uid"]
                    ),
                }
            )
    timeline.sort(
        key=lambda row: (
            str(row["time_exact"] or "9999"),
            str(row["row_kind"]),
            str(row["timeline_row_uid"]),
        )
    )
    atomic_parquet(
        canonical / "wikidisputes_full_event_timeline.parquet",
        pa.Table.from_pylist(timeline),
    )

    utterances_path = canonical / "wikidisputes_utterances_ssot.parquet"
    if utterances_path.exists():
        atomic_link_or_copy(
            utterances_path,
            canonical / "wikidisputes_logical_utterance_timeline.parquet",
        )
        utterances = pq.read_table(utterances_path).to_pylist()
        common_support = [
            row
            for row in utterances
            if isinstance(row.get("created_at_utc"), str)
            and "2012-" <= row["created_at_utc"][:5] <= "2018-"
        ]
        atomic_parquet(
            analysis / "common_support_2012_2018.parquet", pa.Table.from_pylist(common_support)
        )

    database = output_root / "wikidisputes_ssot.duckdb"
    temporary_db = database.with_suffix(".duckdb.tmp")
    temporary_db.unlink(missing_ok=True)
    connection = duckdb.connect(str(temporary_db))
    try:
        for directory in (silver, canonical, analysis):
            for path in sorted(directory.glob("*.parquet")):
                view = path.stem.replace("-", "_")
                escaped = str(path.resolve()).replace("'", "''")
                connection.execute(
                    f"CREATE OR REPLACE VIEW \"{view}\" AS SELECT * FROM read_parquet('{escaped}')"
                )
        projection_path = str(
            (canonical / "wikidisputes_source_projection.parquet").resolve()
        ).replace("'", "''")
        for side in ("escalated", "non_escalated"):
            connection.execute(
                f'CREATE OR REPLACE VIEW "source_projection_{side}_order" AS '
                f"SELECT * FROM read_parquet('{projection_path}') "
                f"WHERE source_side = '{side}' "
                "ORDER BY source_case_index, source_row_index"
            )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    os.replace(temporary_db, database)

    def descriptors(paths: list[Path]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in paths:
            descriptor = file_descriptor(path)
            descriptor["rows"] = pq.read_metadata(path).num_rows
            descriptor["path"] = str(path.relative_to(output_root))
            result.append(descriptor)
        return result

    canonical_artifact_paths = sorted(canonical.glob("*.parquet")) + sorted(
        analysis.glob("*.parquet")
    )
    all_artifact_paths = sorted(silver.glob("*.parquet")) + canonical_artifact_paths
    artifacts = descriptors(canonical_artifact_paths)
    all_artifacts = descriptors(all_artifact_paths)
    code_root = repository_root or Path.cwd()
    code_files = sorted((code_root / "src").rglob("*.py"))
    code_build_sha256 = canonical_json_hash(
        {str(path.relative_to(code_root)): sha256_file(path) for path in code_files}
    )
    canonical_manifest = {
        "schema_version": SCHEMA_VERSION,
        "identity_algorithm_version": IDENTITY_VERSION,
        "representation_version": REPRESENTATION_VERSION,
        "join_contract_version": JOIN_CONTRACT_VERSION,
        "dv_definition_version": DV_VERSION,
        "canonical_config_sha256": canonical_json_hash(canonical_config),
        "code_build_sha256": code_build_sha256,
        "artifacts": artifacts,
    }
    atomic_write_json(output_root / "manifests" / "canonical_outputs.json", canonical_manifest)
    run_manifest = {
        **canonical_manifest,
        "all_retrieval_and_canonical_artifacts": all_artifacts,
        "generated_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "database": file_descriptor(database),
        "note": "retrieval/run time is provenance only and excluded from canonical table contents",
    }
    atomic_write_json(output_root / "manifests" / "run.json", run_manifest)
    return run_manifest
