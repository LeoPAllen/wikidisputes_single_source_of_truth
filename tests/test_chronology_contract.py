from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from wikidisputes_ssot import cli
from wikidisputes_ssot.export import build_hashes

ROOT = Path(__file__).resolve().parents[1]


def _chronology_hash(rows: list[dict[str, object]]) -> str:
    eligible = [
        row
        for row in rows
        if row.get("chronology_eligible") is True and row.get("chronology_rank") is not None
    ]
    ordered = sorted(
        eligible,
        key=lambda row: (
            str(row["conversation_uid"]),
            int(row["chronology_rank"]),
            str(row["logical_utterance_uid"]),
        ),
    )
    payload = json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def test_chronology_hash_ignores_input_and_display_order() -> None:
    rows = [
        {
            "conversation_uid": "c1",
            "logical_utterance_uid": "u2",
            "chronology_eligible": True,
            "chronology_rank": 2,
            "display_order": 3,
        },
        {
            "conversation_uid": "c1",
            "logical_utterance_uid": "u1",
            "chronology_eligible": True,
            "chronology_rank": 1,
            "display_order": 1,
        },
        {
            "conversation_uid": "c1",
            "logical_utterance_uid": "u3",
            "chronology_eligible": False,
            "chronology_rank": None,
            "display_order": 2,
        },
    ]
    assert _chronology_hash(rows) == _chronology_hash(list(reversed(rows)))


def test_clean_and_resumed_runs_use_same_post_hydration_chronology_hash(
    tmp_path: Path, monkeypatch
) -> None:
    state = {"revision_timestamps_hydrated": False}
    settings = SimpleNamespace(
        roots=SimpleNamespace(data=tmp_path / "data", output=tmp_path / "output"),
        network=SimpleNamespace(user_agent="fixture"),
        run=SimpleNamespace(review_seed=7),
        wikiconv=SimpleNamespace(years=[]),
        canonical_dict=lambda: {"fixture": True},
    )
    monkeypatch.setattr(cli, "_settings", lambda _config: settings)
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)

    def static_stage(*_args, **_kwargs):
        return {"status": "fixture"}

    for name in (
        "download_pin",
        "extract_archive",
        "build_source_projection",
        "audit_source",
        "materialize_source_lineage",
        "materialize_source_core",
        "extract_historical_article_edits",
        "materialize_literature_registry",
        "materialize_replication_views",
        "merge_enumeration",
        "materialize_events_and_dvs",
        "hydrate_article_histories",
        "recover_revision_representations",
        "hydrate_selected_parses",
        "materialize_review_packet",
        "materialize_exports",
        "validate_all",
    ):
        monkeypatch.setattr(cli, name, static_stage)

    def hydrate(*_args, **_kwargs):
        state["revision_timestamps_hydrated"] = True
        return {"status": "fixture_hydrated"}

    def rehydrate(*_args, **_kwargs):
        suffix = "hydrated" if state["revision_timestamps_hydrated"] else "bootstrap"
        return {"chronology_relevant_artifact_hashes": {"utterances": suffix}}

    monkeypatch.setattr(cli, "hydrate_selected_revisions", hydrate)
    monkeypatch.setattr(cli, "materialize_full_rehydrated", rehydrate)

    clean = cli._run_all(tmp_path / "config.yaml")
    clean_final = clean["full_rehydration_final"]
    assert clean["full_rehydration_bootstrap"] != clean_final
    assert clean["pipeline_sequence"].index("mediawiki_revisions") < clean[
        "pipeline_sequence"
    ].index("full_rehydration_final")

    resumed = cli._run_all(tmp_path / "config.yaml")
    assert resumed["full_rehydration_final"] == clean_final


def test_schema_declares_strict_derivative_and_nullable_rank() -> None:
    schema = yaml.safe_load((ROOT / "schemas" / "tables.yaml").read_bytes())
    utterances = schema["tables"]["utterances"]
    assert "chronology_rank" in utterances["required"]
    assert "display_utterance_order" in utterances["required"]
    assert "canonical/wikidisputes_chronology_strict.parquet" in schema["views"]


def test_build_hashes_are_stable_for_same_repository() -> None:
    assert build_hashes(ROOT) == build_hashes(ROOT)
