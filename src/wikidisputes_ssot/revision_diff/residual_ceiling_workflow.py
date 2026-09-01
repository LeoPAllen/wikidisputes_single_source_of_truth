"""Read-only orchestration for the residual recoverability ceiling audit."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from wikidisputes_ssot.config import Settings
from wikidisputes_ssot.hashing import canonical_json_hash
from wikidisputes_ssot.io import atomic_write_json, file_descriptor

from .cache import load_cached_revision_index, resolve_revision_text
from .residual_ceiling import (
    SAMPLE_SIZE,
    SEED,
    SamplePlan,
    derive_residual_rows,
    deterministic_stratified_sample,
    summarize_completed_labels,
)
from .residual_ceiling_audit import build_residual_audit_packet
from .workflow import MethodBPaths


@dataclass(frozen=True, slots=True)
class ResidualCeilingPaths:
    """Generated audit bundle paths; none are production recovery outputs."""

    root: Path
    csv: Path
    html: Path
    manifest: Path
    results: Path

    @classmethod
    def from_settings(cls, settings: Settings, seed: str = SEED) -> ResidualCeilingPaths:
        if not seed.isascii() or not seed.isdigit():
            raise ValueError("seed must contain ASCII digits only")
        root = (
            settings.roots.output / "manual_review" / "revision_diff" / f"residual_ceiling_{seed}"
        )
        return cls(
            root=root,
            csv=root / "audit.csv",
            html=root / "audit.html",
            manifest=root / "manifest.json",
            results=root / "weighted_summary.json",
        )


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    return pq.read_table(path).to_pylist()


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_range(*values: Any) -> tuple[int | None, int | None]:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list) and parsed:
            first = parsed[0]
            if isinstance(first, (list, tuple)) and len(first) == 2:
                return _integer(first[0]), _integer(first[1])
    return None, None


def _discussiontools_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {str(row["source_row_uid"]): row for row in _rows(path)}


def _build_plan(
    paths: MethodBPaths,
    *,
    seed: str,
    sample_size: int,
) -> tuple[SamplePlan, int, Path]:
    residual, total = derive_residual_rows(
        _rows(paths.source_population),
        _rows(paths.recovery_evidence),
        _rows(paths.selection_audit),
    )
    discussiontools_path = (
        paths.source_population.parent / "discussiontools_feasibility_evidence.parquet"
    )
    discussiontools = _discussiontools_rows(discussiontools_path)
    for row in residual:
        uid = str(row["source_row_uid"])
        rendered = discussiontools.get(uid)
        row["token_persistence"] = (
            row.get("predecessor_target_continuity") == "token_persistence_continuity"
        )
        row["discussiontools_evidence"] = rendered is not None
        if rendered is not None:
            for source_name, target_name in (
                ("parser_success", "discussiontools_parser_success"),
                ("exact_boundary_agreement", "discussiontools_exact_boundary_agreement"),
                ("contamination_status", "discussiontools_contamination_status"),
                ("proposed_safe", "discussiontools_proposed_safe"),
                ("failure_reasons", "discussiontools_failure_reasons"),
                ("discussiontools_state_status", "discussiontools_state_status"),
            ):
                row[target_name] = rendered.get(source_name)
    return (
        deterministic_stratified_sample(
            residual,
            requested_size=sample_size,
            seed=seed,
            min_per_stratum=20,
        ),
        total,
        discussiontools_path,
    )


def _raw_texts(
    settings: Settings, paths: MethodBPaths, rows: list[dict[str, Any]]
) -> dict[int, str]:
    revision_ids: set[int] = set()
    for row in rows:
        for field in ("target_revision_id", "revision_id", "predecessor_revision_id"):
            revision_id = _integer(row.get(field))
            if revision_id not in (None, 0):
                revision_ids.add(revision_id)
    records = load_cached_revision_index(settings, paths.revision_index, required_ids=revision_ids)
    raw: dict[int, str] = {0: ""}
    for revision_id in sorted(revision_ids):
        record = records.get(revision_id)
        if record is None:
            continue
        resolved = resolve_revision_text(settings, record)
        if resolved.raw_text is not None:
            raw[revision_id] = resolved.raw_text
    return raw


def _audit_source(row: dict[str, Any], raw: dict[int, str]) -> dict[str, Any]:
    target_id = _integer(row.get("target_revision_id") or row.get("revision_id"))
    predecessor_id = _integer(row.get("predecessor_revision_id"))
    if target_id is None or target_id not in raw:
        raise RuntimeError(f"sampled target revision is not available in exact cache: {target_id}")
    target_start, target_end = _first_range(
        row.get("action_target_changed_ranges_json"), row.get("target_changed_ranges_json")
    )
    predecessor_start, predecessor_end = _first_range(row.get("predecessor_changed_ranges_json"))
    keep = (
        "source_row_uid",
        "source_text",
        "target_text",
        "method_b_status",
        "lifecycle",
        "failure_reasons",
        "primary_stratum",
        "design_cell",
        "diagnostic_domain",
        "population_n",
        "sample_n",
        "inclusion_probability",
        "survey_weight",
        "year",
        "target_length",
        "multi_action_revision",
        "action_count_in_revision",
        "action_type",
        "diff_operations_json",
        "target_changed_ranges_json",
        "predecessor_changed_ranges_json",
        "action_target_changed_ranges_json",
        "hunk_attribution_evidence_json",
        "candidate_start",
        "candidate_end",
        "candidate_raw",
        "body_start",
        "body_end",
        "candidate_body",
        "competing_candidates_json",
        "competing_actions_json",
        "boundary_evidence_json",
        "signature_timestamp",
        "signature_author",
        "revision_actor",
        "wikiconv_speaker",
        "wikidisputes_speaker",
        "action_offset_hint",
        "assignment_status",
        "assignment_evidence_json",
        "assignment_conflicts_json",
        "lifecycle_consistency",
        "predecessor_target_continuity",
        "discussiontools_evidence",
        "discussiontools_parser_success",
        "discussiontools_exact_boundary_agreement",
        "discussiontools_contamination_status",
        "discussiontools_proposed_safe",
        "discussiontools_failure_reasons",
        "discussiontools_state_status",
    )
    return {
        **{field: row.get(field) for field in keep},
        "audit_uid": "residual-ceiling:" + canonical_json_hash(row["source_row_uid"])[:20],
        "target_wikitext": raw[target_id],
        "predecessor_wikitext": raw.get(predecessor_id or 0, ""),
        "target_changed_start": target_start,
        "target_changed_end": target_end,
        "predecessor_changed_start": predecessor_start,
        "predecessor_changed_end": predecessor_end,
    }


def _diagnostic_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    years = sorted(int(row["year"]) for row in rows if row.get("year") is not None)
    lengths = sorted(
        int(row["target_length"]) for row in rows if row.get("target_length") is not None
    )
    return {
        "b_status": dict(sorted(Counter(row["method_b_status"] for row in rows).items())),
        "lifecycle": dict(sorted(Counter(row["lifecycle"] for row in rows).items())),
        "multi_action": sum(bool(row.get("multi_action_revision")) for row in rows),
        "diff_span": sum(row.get("diff_operations_json") not in (None, "", "[]") for row in rows),
        "token_persistence": sum(bool(row.get("token_persistence")) for row in rows),
        "discussiontools_evidence": sum(bool(row.get("discussiontools_evidence")) for row in rows),
        "year": {"min": min(years), "max": max(years), "distinct": len(set(years))},
        "target_length": {
            "min": min(lengths),
            "median": lengths[len(lengths) // 2],
            "max": max(lengths),
        },
    }


def build_residual_ceiling_packet(
    settings: Settings,
    *,
    seed: str = SEED,
    sample_size: int = SAMPLE_SIZE,
    excerpt_limit: int = 900,
) -> dict[str, Any]:
    """Write the generated 600-row audit bundle without touching recovery outputs."""

    method_paths = MethodBPaths.from_settings(settings)
    output_paths = ResidualCeilingPaths.from_settings(settings, seed)
    plan, total_population, discussiontools_path = _build_plan(
        method_paths, seed=seed, sample_size=sample_size
    )
    sampled = list(plan.sampled)
    raw = _raw_texts(settings, method_paths, sampled)
    audit_rows = [_audit_source(row, raw) for row in sampled]
    safe_metadata = {
        "seed": seed,
        "sample_size": len(sampled),
        "label_command": "Use the residual-ceiling-label command; each row is saved atomically.",
    }
    artifact = build_residual_audit_packet(
        audit_rows,
        csv_path=output_paths.csv,
        html_path=output_paths.html,
        metadata=safe_metadata,
        excerpt_limit=excerpt_limit,
    )
    manifest = {
        "status": "unadjudicated",
        "seed": seed,
        "requested_sample_size": sample_size,
        "sample_size": len(sampled),
        "total_population": total_population,
        "validated_a_plus_b": total_population - len(plan.frame),
        "residual_total": len(plan.frame),
        "b_unavailable_exact": plan.unavailable_count,
        "eligible_residual": len(plan.frame) - plan.unavailable_count,
        "primary_strata": list(plan.primary_strata),
        "design_cells": list(plan.design_cells),
        "diagnostic_representation": _diagnostic_counts(sampled),
        "sample_source_uid_sha256": canonical_json_hash([row["source_row_uid"] for row in sampled]),
        "inputs": {
            "source_population": file_descriptor(method_paths.source_population),
            "recovery_evidence": file_descriptor(method_paths.recovery_evidence),
            "selection_audit": file_descriptor(method_paths.selection_audit),
            "discussiontools_evidence": (
                file_descriptor(discussiontools_path) if discussiontools_path.exists() else None
            ),
        },
        "artifact": artifact,
    }
    atomic_write_json(output_paths.manifest, manifest)
    return {**manifest, "manifest_path": str(output_paths.manifest)}


def summarize_residual_ceiling(settings: Settings, *, seed: str = SEED) -> dict[str, Any]:
    """Validate the completed mutable CSV and write its weighted summary."""

    method_paths = MethodBPaths.from_settings(settings)
    output_paths = ResidualCeilingPaths.from_settings(settings, seed)
    manifest = json.loads(output_paths.manifest.read_text(encoding="utf-8"))
    sample_size = int(manifest["sample_size"])
    plan, total_population, _ = _build_plan(method_paths, seed=seed, sample_size=sample_size)
    csv.field_size_limit(sys.maxsize)
    with output_paths.csv.open(newline="", encoding="utf-8") as handle:
        audit_rows = list(csv.DictReader(handle))
    sampled_uids = [str(row["source_row_uid"]) for row in plan.sampled]
    csv_uids = [row["source_row_uid"] for row in audit_rows]
    if sampled_uids != csv_uids:
        raise RuntimeError("audit CSV identities/order do not match the frozen sample design")
    if canonical_json_hash(sampled_uids) != manifest["sample_source_uid_sha256"]:
        raise RuntimeError("sample identity hash does not match manifest")
    labels = {row["source_row_uid"]: row for row in audit_rows}
    report = summarize_completed_labels(plan, labels, total_population=total_population)
    report["artifacts"] = {
        "audit_csv": file_descriptor(output_paths.csv),
        "audit_html": file_descriptor(output_paths.html),
        "manifest": file_descriptor(output_paths.manifest),
    }
    atomic_write_json(output_paths.results, report)
    return {**report, "results_path": str(output_paths.results)}
