from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_acceptance_ids_are_unique_and_status_enum_is_complete() -> None:
    matrix = yaml.safe_load((ROOT / "schemas" / "acceptance_matrix.yaml").read_bytes())
    identifiers = [gate["id"] for gate in matrix["gates"]]
    assert len(identifiers) == len(set(identifiers))
    assert {
        "pass",
        "fail",
        "blocked_source",
        "blocked_retrieval",
        "human_validation_required",
    } <= set(matrix["status_enum"])


def test_machine_schema_contains_every_required_entity() -> None:
    schema = yaml.safe_load((ROOT / "schemas" / "tables.yaml").read_bytes())
    required = {
        "source_manifests",
        "source_files",
        "source_rows",
        "selected_conversations",
        "conversation_source_membership",
        "disputes",
        "dispute_episodes",
        "episode_threads",
        "context_nodes",
        "utterances",
        "utterance_actions",
        "utterance_versions",
        "utterance_representations",
        "source_id_aliases",
        "identity_registry",
        "reply_edges",
        "authors_actors",
        "signatures",
        "links",
        "article_revisions",
        "events",
        "event_evidence",
        "outcomes",
        "dv_definitions",
        "annotation_join_contract",
        "quality_flags",
        "literature_cleaning_registry",
    }
    assert required <= set(schema["tables"])
