"""Read-only orchestration for deterministic residual rule probes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from wikidisputes_ssot.config import Settings
from wikidisputes_ssot.hashing import canonical_json_hash
from wikidisputes_ssot.io import (
    atomic_parquet,
    atomic_write_json,
    file_descriptor,
    table_from_union_pylist,
)

from .llm_audit_bundle import LLMAuditBundlePaths
from .residual_ceiling import SEED
from .residual_ceiling_workflow import ResidualCeilingPaths
from .rule_probes import run_probes, summarize_probe_results

FROZEN_SAMPLE_SIZE = 600
FROZEN_SAMPLE_UID_HASH = "5135ac57e21a59f7b0e68ea26eef2508420eec89f7cc822839ddea438916b5d5"


@dataclass(frozen=True, slots=True)
class RuleProbePaths:
    root: Path
    rows: Path
    summary: Path

    @classmethod
    def from_settings(cls, settings: Settings, seed: str = SEED) -> RuleProbePaths:
        root = ResidualCeilingPaths.from_settings(settings, seed).root / "rule_probes"
        return cls(
            root=root,
            rows=root / "frozen_600_rule_probe_results.parquet",
            summary=root / "frozen_600_rule_probe_summary.json",
        )


def run_residual_rule_probe(
    settings: Settings,
    *,
    seed: str = SEED,
    input_path: Path | None = None,
    output_directory: Path | None = None,
) -> dict[str, Any]:
    """Evaluate proofs over an existing evidence parquet without recovery writes."""

    default_input = LLMAuditBundlePaths.from_settings(settings, seed).sample_evidence
    source_path = input_path or default_input
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    rows = pq.read_table(source_path).to_pylist()
    uids = [str(row.get("source_row_uid") or "") for row in rows]
    if not all(uids) or len(set(uids)) != len(uids):
        raise RuntimeError("probe input must contain unique nonempty source_row_uid values")

    frozen_sample = source_path.resolve() == default_input.resolve()
    uid_hash = canonical_json_hash(uids)
    if frozen_sample:
        if len(rows) != FROZEN_SAMPLE_SIZE or uid_hash != FROZEN_SAMPLE_UID_HASH:
            raise RuntimeError("frozen 600 sample identity/order mismatch")
        manifest_path = LLMAuditBundlePaths.from_settings(settings, seed).manifest
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("sample_uid_hash") != FROZEN_SAMPLE_UID_HASH:
            raise RuntimeError("LLM audit bundle manifest sample hash mismatch")

    results = run_probes(rows)
    if len(results) != len(rows) * 5:
        raise RuntimeError("expected exactly five probe results per input row")
    design = {
        str(row["source_row_uid"]): (
            row.get("inclusion_probability"),
            row.get("survey_weight"),
        )
        for row in rows
    }
    for result in results:
        expected = design[str(result["source_row_uid"])]
        if (result.get("inclusion_probability"), result.get("survey_weight")) != expected:
            raise RuntimeError("probe result changed frozen survey-design values")
    summary = summarize_probe_results(rows, results)
    summary.update(
        {
            "diagnostic_only": True,
            "input": file_descriptor(source_path),
            "input_rows": len(rows),
            "sample_uid_hash": uid_hash,
            "frozen_sample_verified": frozen_sample,
            "seed": seed,
        }
    )

    default_paths = RuleProbePaths.from_settings(settings, seed)
    if output_directory is None:
        paths = default_paths
    else:
        paths = RuleProbePaths(
            root=output_directory,
            rows=output_directory / "rule_probe_results.parquet",
            summary=output_directory / "rule_probe_summary.json",
        )
    table = table_from_union_pylist(results)
    atomic_parquet(paths.rows, table)
    summary["outputs"] = {
        "row_results": {**file_descriptor(paths.rows), "rows": table.num_rows},
        "summary": str(paths.summary),
    }
    atomic_write_json(paths.summary, summary)
    return {
        "rows": str(paths.rows),
        "summary": str(paths.summary),
        "input_rows": len(rows),
        "probe_rows": len(results),
        "rule_families": summary["rule_families"],
        "overlaps": summary["overlaps"],
        "frozen_sample_verified": frozen_sample,
    }
