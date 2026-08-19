from __future__ import annotations

import datetime as dt
import os
import tempfile
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from .constants import (
    DV_VERSION,
    IDENTITY_VERSION,
    JOIN_CONTRACT_VERSION,
    REPRESENTATION_VERSION,
    SCHEMA_VERSION,
)
from .hashing import canonical_json_hash, sha256_file
from .io import atomic_link_or_copy, atomic_write_json, file_descriptor


def _duckdb_copy(query: str, target: Path) -> None:
    """Atomically stream a deterministic ordered query to compressed Parquet."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    temporary_path.unlink()
    connection = duckdb.connect()
    try:
        escaped = str(temporary_path.resolve()).replace("'", "''")
        connection.execute(
            f"COPY ({query}) TO '{escaped}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)"
        )
        os.replace(temporary_path, target)
    finally:
        connection.close()
        temporary_path.unlink(missing_ok=True)


def _parquet_sql(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


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

    action_sql_path = _parquet_sql(silver / "utterance_actions.parquet")
    context_actions_path = silver / "context_actions.parquet"
    events_sql_path = _parquet_sql(silver / "events.parquet")
    timeline_parts = [
        (
            "SELECT CAST(action_uid AS VARCHAR) AS timeline_row_uid, "
            "'utterance_action' AS row_kind, CAST(action_type AS VARCHAR) "
            "AS event_or_action_type, CAST(NULL AS VARCHAR) AS episode_uid, "
            "CAST(logical_utterance_uid AS VARCHAR) AS logical_utterance_uid, "
            "CAST(NULL AS VARCHAR) AS context_node_uid, CAST(raw_timestamp AS VARCHAR) "
            "AS time_exact, 'source_or_wikiconv_exact' AS time_status, "
            "COALESCE(CAST(source_row_uid AS VARCHAR), "
            "'action:' || CAST(action_uid AS VARCHAR)) AS evidence_pointer "
            f"FROM read_parquet('{action_sql_path}')"
        ),
        (
            "SELECT CAST(event_uid AS VARCHAR), 'event', CAST(event_type AS VARCHAR), "
            "CAST(episode_uid AS VARCHAR), CAST(NULL AS VARCHAR), CAST(NULL AS VARCHAR), "
            "COALESCE(CAST(event_time_utc AS VARCHAR), CAST(event_time_exact AS VARCHAR)), "
            "CAST(event_time_status AS VARCHAR), 'event:' || CAST(event_uid AS VARCHAR) "
            f"FROM read_parquet('{events_sql_path}')"
        ),
    ]
    if context_actions_path.exists():
        context_sql_path = _parquet_sql(context_actions_path)
        timeline_parts.append(
            "SELECT CAST(context_action_uid AS VARCHAR), 'context_action', "
            "CAST(action_type AS VARCHAR), CAST(NULL AS VARCHAR), CAST(NULL AS VARCHAR), "
            "CAST(context_node_uid AS VARCHAR), CAST(raw_timestamp AS VARCHAR), "
            "'wikiconv_exact', COALESCE(CAST(wikiconv_source_row_uid AS VARCHAR), "
            "'context_action:' || CAST(context_action_uid AS VARCHAR)) "
            f"FROM read_parquet('{context_sql_path}')"
        )
    article_history_path = silver / "article_revision_observations.parquet"
    if article_history_path.exists():
        article_sql_path = _parquet_sql(article_history_path)
        timeline_parts.append(
            "SELECT CAST(article_revision_observation_uid AS VARCHAR), 'event', "
            "'article_edit', CAST(NULL AS VARCHAR), CAST(NULL AS VARCHAR), "
            "CAST(NULL AS VARCHAR), CAST(timestamp AS VARCHAR), "
            "'mediawiki_revision_timestamp', 'article_revision_observation:' || "
            "CAST(article_revision_observation_uid AS VARCHAR) "
            f"FROM read_parquet('{article_sql_path}')"
        )
    _duckdb_copy(
        "SELECT * FROM ("
        + " UNION ALL ".join(timeline_parts)
        + ") ORDER BY time_exact NULLS LAST, row_kind, timeline_row_uid",
        canonical / "wikidisputes_full_event_timeline.parquet",
    )

    utterances_path = canonical / "wikidisputes_utterances_ssot.parquet"
    if utterances_path.exists():
        atomic_link_or_copy(
            utterances_path,
            canonical / "wikidisputes_logical_utterance_timeline.parquet",
        )
        utterance_sql_path = _parquet_sql(utterances_path)
        _duckdb_copy(
            "SELECT * "
            f"FROM read_parquet('{utterance_sql_path}') "
            "WHERE CAST(created_at_utc AS VARCHAR) >= '2012-' "
            "AND CAST(created_at_utc AS VARCHAR) < '2019-' "
            "ORDER BY conversation_uid, utterance_order, logical_utterance_uid",
            analysis / "common_support_2012_2018.parquet",
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
