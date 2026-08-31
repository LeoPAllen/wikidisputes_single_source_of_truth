"""Artifact orchestration for the additive Method-B workflow."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from wikidisputes_ssot.config import Settings
from wikidisputes_ssot.hashing import canonical_json_hash
from wikidisputes_ssot.io import (
    atomic_parquet,
    atomic_write_bytes,
    atomic_write_json,
    file_descriptor,
    table_from_union_pylist,
)
from wikidisputes_ssot.source_provenance import check_source_text_provenance

from .assignment import AssignmentConfig
from .boundaries import extract_comment_candidates
from .cache import (
    CachedRevision,
    hydrate_revision_pairs,
    load_cached_revision_index,
    resolve_revision_text,
)
from .models import RevisionAvailability, RevisionText, local_content_sha256
from .pilot_comparison import boundary_usable_fix_comparison_report
from .recovery import evidence_as_rows, recover_revision_actions
from .reporting import (
    blinded_audit_packet,
    localization_fix_comparison_report,
    pilot_validation_report,
    profile_rows,
    recovery_report,
    select_stratified_pilot,
)
from .safety import SOFT_USABILITY_REASONS

WORKFLOW_VERSION = "method-b-workflow-v6-diff-span-structural"

METHOD_B_SELECTABLE_STATUSES = frozenset({"b_safe", "b_usable"})


@dataclass(frozen=True, slots=True)
class MethodBPaths:
    source_population: Path
    profile: Path
    profile_report: Path
    pilot_population: Path
    pilot_manifest: Path
    revision_index: Path
    revision_pairs: Path
    revision_history: Path
    recovery_evidence: Path
    representations: Path
    recovery_report: Path
    pilot_recovery_evidence: Path
    pilot_representations: Path
    pilot_recovery_report: Path
    pilot_validation: Path
    pilot_validation_report: Path
    localization_fix_comparison: Path
    boundary_usable_fix_comparison: Path
    pre_boundary_usable_fix_validation: Path
    pre_boundary_usable_fix_evidence: Path
    pre_boundary_usable_fix_audit_key: Path
    selection_audit: Path
    combined_representation: Path
    selection_report: Path
    audit_packet: Path
    audit_key: Path
    audit_manifest: Path
    pilot_audit_packet: Path
    pilot_audit_key: Path
    pilot_audit_manifest: Path
    staged_annotation: Path
    invariants_report: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> MethodBPaths:
        silver = settings.roots.output / "silver"
        reports = settings.roots.output / "reports" / "revision_diff"
        audit = settings.roots.output / "manual_review" / "revision_diff"
        annotation = settings.roots.output / "annotation"
        return cls(
            source_population=silver / "method_b_source_population.parquet",
            profile=silver / "method_b_population_profile.parquet",
            profile_report=reports / "method_b_population_profile.json",
            pilot_population=silver / "method_b_pilot_population.parquet",
            pilot_manifest=reports / "method_b_pilot_manifest.json",
            revision_index=silver / "method_b_revision_content_index.parquet",
            revision_pairs=silver / "method_b_revision_pairs.parquet",
            revision_history=silver / "method_b_revision_history.parquet",
            recovery_evidence=silver / "method_b_recovery_evidence.parquet",
            representations=silver / "method_b_representations.parquet",
            recovery_report=reports / "method_b_recovery_report.json",
            pilot_recovery_evidence=silver / "method_b_pilot_recovery_evidence.parquet",
            pilot_representations=silver / "method_b_pilot_representations.parquet",
            pilot_recovery_report=reports / "method_b_pilot_recovery_report.json",
            pilot_validation=reports / "method_b_pilot_validation.parquet",
            pilot_validation_report=reports / "method_b_pilot_validation.json",
            localization_fix_comparison=(reports / "method_b_localization_fix_comparison.json"),
            boundary_usable_fix_comparison=(
                reports / "method_b_boundary_usable_fix_comparison.json"
            ),
            pre_boundary_usable_fix_validation=(
                reports
                / "pre_boundary_usable_fix"
                / "reports"
                / "method_b_pilot_validation.parquet"
            ),
            pre_boundary_usable_fix_evidence=(
                reports
                / "pre_boundary_usable_fix"
                / "silver"
                / "method_b_pilot_recovery_evidence.parquet"
            ),
            pre_boundary_usable_fix_audit_key=(
                reports
                / "pre_boundary_usable_fix"
                / "manual_review"
                / "method_b_pilot_blinded_audit_key.parquet"
            ),
            selection_audit=reports / "method_b_selection_audit.parquet",
            combined_representation=silver / "method_b_combined_representation.parquet",
            selection_report=reports / "method_b_selection_report.json",
            audit_packet=audit / "method_b_blinded_audit_packet.parquet",
            audit_key=audit / "method_b_blinded_audit_key.parquet",
            audit_manifest=audit / "method_b_audit_strata_manifest.json",
            pilot_audit_packet=audit / "method_b_pilot_blinded_audit_packet.parquet",
            pilot_audit_key=audit / "method_b_pilot_blinded_audit_key.parquet",
            pilot_audit_manifest=audit / "method_b_pilot_audit_strata_manifest.json",
            staged_annotation=annotation / "wikidisputes_llm_annotation_input.method_b.csv",
            invariants_report=reports / "method_b_final_invariants.json",
        )


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _bool(value: Any) -> bool:
    return value is True or _text(value).strip().casefold() in {"1", "true", "yes"}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    return pq.read_table(path).to_pylist()


def _control_identity_value(row: Mapping[str, Any], field: str) -> Any:
    """Return the current-source value corresponding to a baseline identity field."""

    if field == "target_revision_id":
        value = row.get("target_revision_id")
        return row.get("revision_id") if value in (None, "") else value
    return row.get(field)


def partition_baseline_controls(
    baseline_rows: Sequence[Mapping[str, Any]],
    current_source_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], frozenset[str]]:
    """Validate and partition immutable, selectable rows from prior evidence.

    Baseline rows with a non-selectable status are ignored.  Selectable rows are
    retained by reference (so callers can append their fields unchanged) and
    must identify exactly one current source row on every identity field that
    the baseline supplies.
    """

    source_by_uid: dict[str, Mapping[str, Any]] = {}
    for source_row in current_source_rows:
        source_uid = _text(source_row.get("source_row_uid"))
        if not source_uid:
            raise ValueError("current source row is missing source_row_uid")
        if source_uid in source_by_uid:
            raise ValueError(f"duplicate current source_row_uid: {source_uid}")
        source_by_uid[source_uid] = source_row

    controls: list[Mapping[str, Any]] = []
    control_uids: set[str] = set()
    seen_baseline_uids: set[str] = set()
    identity_fields = (
        "source_row_uid",
        "action_uid",
        "logical_utterance_uid",
        "target_revision_id",
    )
    for baseline_row in baseline_rows:
        source_uid = _text(baseline_row.get("source_row_uid"))
        if not source_uid:
            raise ValueError("baseline evidence row is missing source_row_uid")
        if source_uid in seen_baseline_uids:
            raise ValueError(f"duplicate baseline source_row_uid: {source_uid}")
        seen_baseline_uids.add(source_uid)
        if _text(baseline_row.get("status")).casefold() not in METHOD_B_SELECTABLE_STATUSES:
            continue

        current_row = source_by_uid.get(source_uid)
        if current_row is None:
            raise ValueError(
                f"baseline control source_row_uid is absent from current source: {source_uid}"
            )
        for field in identity_fields[1:]:
            baseline_value = baseline_row.get(field)
            if baseline_value in (None, ""):
                continue
            current_value = _control_identity_value(current_row, field)
            if current_value in (None, "") or _text(current_value) != _text(baseline_value):
                raise ValueError(
                    f"baseline control identity mismatch for {source_uid}: "
                    f"{field}={baseline_value!r}, current={current_value!r}"
                )
        controls.append(baseline_row)
        control_uids.add(source_uid)
    return controls, frozenset(control_uids)


def merge_pilot_control_evidence(
    primary_rows: Sequence[Mapping[str, Any]],
    pilot_rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Use pilot controls only when the primary evidence has no such row."""

    merged = list(primary_rows)
    seen: set[str] = set()
    for row in primary_rows:
        source_uid = _text(row.get("source_row_uid"))
        if not source_uid or source_uid in seen:
            raise ValueError(f"duplicate or missing primary source_row_uid: {source_uid!r}")
        seen.add(source_uid)
    primary_uids = frozenset(seen)
    pilot_uids: set[str] = set()
    for row in pilot_rows:
        source_uid = _text(row.get("source_row_uid"))
        if not source_uid:
            raise ValueError("pilot evidence row is missing source_row_uid")
        if source_uid in pilot_uids:
            raise ValueError(f"duplicate pilot source_row_uid: {source_uid}")
        pilot_uids.add(source_uid)
        if source_uid in primary_uids:
            continue
        merged.append(row)
        seen.add(source_uid)
    return merged


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return [part for part in str(value).split("|") if part]
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _year(timestamp: Any) -> int | None:
    value = _text(timestamp)
    return int(value[:4]) if len(value) >= 4 and value[:4].isdigit() else None


def _markup_density(value: str) -> float:
    markers = sum(value.count(marker) for marker in ("[[", "{{", "<ref", "http://", "https://"))
    return markers / max(1, len(value))


def _default_inputs(settings: Settings) -> dict[str, Path]:
    return {
        "method_a_audit": settings.roots.output.parent
        / "reports"
        / "mediawiki_raw_comment_promotion_audit.parquet",
        "method_a_recovery": settings.roots.output
        / "silver"
        / "mediawiki_raw_comment_recovery.parquet",
        "join": settings.roots.output
        / "canonical"
        / "wikidisputes_annotation_join_contract.parquet",
        "actions": settings.roots.output / "silver" / "utterance_actions.parquet",
        "observations": settings.roots.output
        / "silver"
        / "talk_page_revision_observations.parquet",
    }


def build_source_population(
    settings: Settings, *, paths: MethodBPaths | None = None
) -> dict[str, Any]:
    """Derive the population from current Method-A decisions and frozen joins."""

    paths = paths or MethodBPaths.from_settings(settings)
    inputs = _default_inputs(settings)
    method_a = _read_rows(inputs["method_a_audit"])
    recoveries = {
        str(row["source_row_uid"]): row for row in _read_rows(inputs["method_a_recovery"])
    }
    join_rows = {str(row["source_row_uid"]): row for row in _read_rows(inputs["join"])}
    action_rows = _read_rows(inputs["actions"])
    actions_by_uid = {str(row["action_uid"]): row for row in action_rows}
    action_count_by_revision = Counter(
        int(row["revision_id"]) for row in action_rows if row.get("revision_id") is not None
    )
    observation_by_revision = {
        int(row["revision_id"]): row
        for row in _read_rows(inputs["observations"])
        if row.get("revision_id") is not None
    }
    canonical_text = {
        source_uid: row.get("wikidisputes_text_exact") for source_uid, row in join_rows.items()
    }
    provenance = check_source_text_provenance(
        [
            {
                "source_row_uid": row["source_row_uid"],
                "source_text": canonical_text.get(str(row["source_row_uid"])),
            }
            for row in method_a
        ],
        canonical_text,
    )
    provenance.require_ok(label="Method-B source population")

    population: list[dict[str, Any]] = []
    mapping_failures: list[str] = []
    for a_row in method_a:
        source_uid = str(a_row["source_row_uid"])
        join = join_rows.get(source_uid)
        recovery = recoveries.get(source_uid, {})
        if join is None:
            mapping_failures.append(source_uid)
            continue
        action_uid = _text(join.get("action_uid"))
        action = actions_by_uid.get(action_uid)
        if action is None:
            mapping_failures.append(source_uid)
            continue
        revision_id = int(action["revision_id"]) if action.get("revision_id") is not None else None
        observation = observation_by_revision.get(revision_id or -1, {})
        parent_id = observation.get("parent_revision_id")
        parent_observation = observation_by_revision.get(int(parent_id)) if parent_id else None
        target_text = _text(join.get("wikidisputes_text_exact"))
        population.append(
            {
                "source_row_uid": source_uid,
                "logical_utterance_uid": _text(join.get("logical_utterance_uid")),
                "action_uid": action_uid,
                "version_uid": _text(join.get("version_uid")),
                "action_id_exact": _text(action.get("action_id_exact")),
                "action_type": _text(action.get("action_type")),
                "revision_id": revision_id,
                "predecessor_revision_id": int(parent_id) if parent_id is not None else None,
                "page_id": observation.get("page_id"),
                "target_availability": observation.get("availability_status", "not_observed"),
                "predecessor_availability": (
                    parent_observation.get("availability_status")
                    if parent_observation
                    else "exact_empty_root"
                    if parent_id == 0
                    else "not_observed"
                ),
                "parentid_verified": parent_id is not None,
                "revision_available": observation.get("availability_status") == "content_available",
                "predecessor_available": bool(
                    parent_id == 0
                    or (
                        parent_observation
                        and parent_observation.get("availability_status") == "content_available"
                    )
                ),
                "target_response_hash": observation.get("response_content_sha256"),
                "target_content_pointer": observation.get("response_blob_path"),
                "source_text": target_text,
                "target_text": target_text,
                "target_empty": not bool(target_text),
                "wikidisputes_speaker": _text(join.get("wikidisputes_user_exact")) or None,
                "wikiconv_speaker": recovery.get("speaker_id"),
                "revision_actor": observation.get("actor_name_exact"),
                "raw_timestamp": action.get("raw_timestamp"),
                "year": _year(action.get("raw_timestamp")),
                "target_length": len(target_text),
                "markup_density": _markup_density(target_text),
                "method_a_status": _text(a_row.get("decision")),
                "method_a_reasons": _json_list(a_row.get("reasons")),
                "method_a_candidate_raw_body": _text(a_row.get("recovered_candidate")),
                "method_a_candidate_full_raw": _text(recovery.get("recovered_raw_wikitext")),
                "candidate_raw_body": _text(a_row.get("recovered_candidate")),
                "method_a_selected_text": _text(a_row.get("final_text")),
                "method_a_recovery_status": _text(a_row.get("recovery_status")),
                "method_a_left_boundary": recovery.get("raw_start"),
                "method_a_right_boundary": recovery.get("raw_end"),
                "method_a_candidate_available": bool(a_row.get("recovered_candidate")),
                "candidate_available": bool(a_row.get("recovered_candidate")),
                "action_count_in_revision": action_count_by_revision.get(revision_id or -1, 0),
                "multi_action_revision": action_count_by_revision.get(revision_id or -1, 0) > 1,
                "source_provenance_exact": True,
                "in_method_b_primary_population": _text(a_row.get("decision"))
                in {"fallback", "review"},
                "method_b_control": _text(a_row.get("decision")) == "promote",
                "workflow_version": WORKFLOW_VERSION,
            }
        )
    if mapping_failures:
        raise RuntimeError(
            f"{len(mapping_failures)} Method-B population rows could not be mapped exactly: "
            f"{mapping_failures[:5]}"
        )
    population.sort(key=lambda row: row["source_row_uid"])
    atomic_parquet(paths.source_population, table_from_union_pylist(population))
    return {
        "source_population_rows": len(population),
        "primary_population_rows": sum(row["in_method_b_primary_population"] for row in population),
        "control_rows": sum(row["method_b_control"] for row in population),
        "method_a_status_counts": dict(Counter(row["method_a_status"] for row in population)),
        "canonical_source_provenance_mismatches": 0,
        "artifact": {**file_descriptor(paths.source_population), "rows": len(population)},
    }


def profile_population(settings: Settings, *, paths: MethodBPaths | None = None) -> dict[str, Any]:
    paths = paths or MethodBPaths.from_settings(settings)
    source_report = build_source_population(settings, paths=paths)
    source = _read_rows(paths.source_population)
    primary = [row for row in source if _bool(row.get("in_method_b_primary_population"))]
    projected = profile_rows(primary)
    atomic_parquet(paths.profile, table_from_union_pylist(projected))
    dimensions = (
        "method_a_status",
        "lifecycle",
        "revision_available",
        "target_availability",
        "predecessor_available",
        "predecessor_availability",
        "candidate_available",
        "year",
        "target_length_bucket",
        "markup_density_bucket",
        "empty_target",
        "multi_action_revision",
    )
    report = {
        "status": "profile_only_no_recovery_executed",
        "workflow_version": WORKFLOW_VERSION,
        "population": source_report,
        "primary_population_rows": len(primary),
        "dimensions": {
            dimension: [
                {dimension: value, "count": count}
                for value, count in sorted(
                    Counter(row.get(dimension) for row in projected).items(),
                    key=lambda item: _text(item[0]),
                )
            ]
            for dimension in dimensions
        },
        "method_a_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                Counter(
                    reason for row in projected for reason in row.get("method_a_reasons", [])
                ).items()
            )
        ],
        "length_summary": {
            "minimum": min((row["target_length"] for row in projected), default=None),
            "maximum": max((row["target_length"] for row in projected), default=None),
        },
        "markup_density_summary": {
            "minimum": min((row["markup_density"] for row in projected), default=None),
            "maximum": max((row["markup_density"] for row in projected), default=None),
        },
        "artifact": {**file_descriptor(paths.profile), "rows": len(projected)},
    }
    atomic_write_json(paths.profile_report, report)
    return report


def select_pilot(
    settings: Settings,
    *,
    paths: MethodBPaths | None = None,
    seed: int = 20260818,
    per_stratum: int = 25,
) -> dict[str, Any]:
    paths = paths or MethodBPaths.from_settings(settings)
    if not paths.source_population.exists():
        build_source_population(settings, paths=paths)
    source = _read_rows(paths.source_population)
    selected = select_stratified_pilot(source, seed=seed, per_stratum=per_stratum)
    rows = selected.pop("rows")
    atomic_parquet(paths.pilot_population, table_from_union_pylist(rows))
    report = {
        **selected,
        "status": "deterministic_selection_only",
        "selected_rows": len(rows),
        "artifact": {**file_descriptor(paths.pilot_population), "rows": len(rows)},
    }
    atomic_write_json(paths.pilot_manifest, report)
    return report


def hydrate_population(
    settings: Settings,
    *,
    population_path: Path,
    paths: MethodBPaths | None = None,
    allow_network: bool = False,
    max_revisions: int | None = None,
    batch_size: int = 50,
    history_depth: int = 0,
    include_controls: bool = False,
) -> dict[str, Any]:
    paths = paths or MethodBPaths.from_settings(settings)
    population = _read_rows(population_path)
    target_ids = [
        int(row["revision_id"])
        for row in population
        if row.get("revision_id") is not None
        and (
            _bool(row.get("in_method_b_primary_population"))
            or (include_controls and _bool(row.get("method_b_control")))
        )
    ]
    return hydrate_revision_pairs(
        settings,
        target_ids,
        index_path=paths.revision_index,
        pairs_path=paths.revision_pairs,
        history_path=paths.revision_history,
        allow_network=allow_network,
        max_revisions=max_revisions,
        batch_size=batch_size,
        history_depth=history_depth,
    )


def _unavailable_revision(revision_id: int | None) -> RevisionText:
    return RevisionText(str(revision_id or "unknown"), RevisionAvailability.UNAVAILABLE, None)


def _history_body_hashes(
    settings: Settings,
    target_id: int,
    history_by_target: Mapping[int, Sequence[Mapping[str, Any]]],
    records: Mapping[int, CachedRevision],
) -> tuple[tuple[str, ...], bool]:
    hashes: set[str] = set()
    rows = history_by_target.get(target_id, ())
    complete = bool(rows)
    for row in rows:
        revision_id = row.get("ancestor_revision_id")
        record = records.get(int(revision_id)) if revision_id is not None else None
        if record is None or record.availability_status != "content_available":
            complete = False
            continue
        revision = resolve_revision_text(settings, record)
        if revision.raw_text is None:
            complete = False
            continue
        hashes.update(
            local_content_sha256(candidate.body_wikitext)
            for candidate in extract_comment_candidates(revision.raw_text)
        )
    return tuple(sorted(hashes)), complete


def _population_signature(
    rows: Sequence[Mapping[str, Any]],
    *,
    attribution_context: Sequence[Mapping[str, Any]] = (),
) -> str:
    return canonical_json_hash(
        {
            "selected": [
                [row.get("source_row_uid"), row.get("action_uid"), row.get("revision_id")]
                for row in sorted(rows, key=lambda item: str(item.get("source_row_uid")))
            ],
            "revision_attribution_context": [
                [
                    row.get("source_row_uid"),
                    row.get("action_uid"),
                    row.get("revision_id"),
                    row.get("action_type"),
                    row.get("action_id_exact"),
                    row.get("source_text"),
                ]
                for row in sorted(
                    attribution_context,
                    key=lambda item: (
                        str(item.get("source_row_uid")),
                        str(item.get("action_uid")),
                    ),
                )
            ],
        }
    )


def _markup_categories(source: str, recovered: str) -> list[str]:
    patterns = {
        "url": ("http://", "https://"),
        "link": ("[[",),
        "template": ("{{",),
        "ref": ("<ref",),
        "diff_markup": ("Special:Diff", "diff=", "oldid="),
    }
    return [
        name
        for name, markers in patterns.items()
        if any(recovered.count(marker) > source.count(marker) for marker in markers)
    ]


def _markup_additions(source: str, recovered: str) -> dict[str, int]:
    patterns = {
        "url": ("http://", "https://"),
        "link": ("[[",),
        "template": ("{{",),
        "ref": ("<ref",),
        "diff_markup": ("Special:Diff", "diff=", "oldid="),
    }
    return {
        name: difference
        for name, markers in patterns.items()
        if (
            difference := sum(recovered.count(marker) for marker in markers)
            - sum(source.count(marker) for marker in markers)
        )
        > 0
    }


def _representations(
    evidence: Sequence[Mapping[str, Any]], population: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in evidence:
        source_uid = str(row["source_row_uid"])
        source = population[source_uid]
        common = {
            "logical_utterance_uid": row.get("logical_utterance_uid"),
            "version_uid": source.get("version_uid"),
            "source_row_uid": source_uid,
            "source_revision_id": row.get("target_revision_id"),
            "predecessor_revision_id": row.get("predecessor_revision_id"),
            "extraction_method": "mediawiki_revision_diff_structural_assignment",
            "extraction_version": WORKFLOW_VERSION,
            "promotion_safety_decision": row.get("status"),
            "promotion_safety_reasons_json": row.get("reason_codes_json"),
            "leakage_class": "source_available",
        }
        specs = []
        if row.get("candidate_raw"):
            specs.append(
                (
                    "mediawiki_revision_diff_comment_wikitext_raw",
                    row["candidate_raw"],
                    "archival_full_comment_including_signature",
                    "recovered_candidate",
                )
            )
        if row.get("candidate_body"):
            safe = row.get("status") == "b_safe"
            specs.append(
                (
                    "mediawiki_revision_diff_comment_wikitext_body"
                    if safe
                    else "mediawiki_revision_diff_comment_wikitext_body_candidate",
                    row["candidate_body"],
                    "annotation_body_signature_removed"
                    if safe
                    else "review_candidate_signature_removed",
                    "recovered" if safe else str(row.get("status")),
                )
            )
        for kind, content, scope, availability in specs:
            output.append(
                {
                    "representation_uid": "wdrepr:method-b:"
                    + canonical_json_hash(
                        [source.get("version_uid"), source_uid, kind, WORKFLOW_VERSION]
                    ),
                    **common,
                    "representation_kind": kind,
                    "representation_scope": scope,
                    "availability_status": availability,
                    "content_sha256": local_content_sha256(str(content)),
                    "byte_length": len(str(content).encode("utf-8")),
                    "encoding": "utf-8",
                    "mime_type": "text/x-wiki",
                    "content_inline": content,
                }
            )
    return output


def recover_population(
    settings: Settings,
    *,
    population_path: Path,
    baseline_evidence_path: Path | None = None,
    paths: MethodBPaths | None = None,
    checkpoint_every: int = 250,
    resume: bool = True,
    max_revisions: int | None = None,
    max_trace_cells: int = 2_000_000,
    include_controls: bool = False,
    ambiguity_tolerance: int = 1,
    max_assignment_actions: int = 10,
    max_assignment_candidates: int = 30,
    max_assignment_edges: int = 200,
    max_assignment_states: int = 100_000,
) -> dict[str, Any]:
    """Run cache-only local reconstruction with atomic deterministic shards."""

    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be positive")
    paths = paths or MethodBPaths.from_settings(settings)
    evidence_path = paths.pilot_recovery_evidence if include_controls else paths.recovery_evidence
    representations_path = (
        paths.pilot_representations if include_controls else paths.representations
    )
    report_path = paths.pilot_recovery_report if include_controls else paths.recovery_report
    source = _read_rows(population_path)
    # Read and validate the immutable baseline before any recovery artifact can
    # be written.  Non-selectable baseline rows are deliberately ignored.
    baseline_descriptor = (
        file_descriptor(baseline_evidence_path) if baseline_evidence_path else None
    )
    baseline_rows = _read_rows(baseline_evidence_path) if baseline_evidence_path else []
    baseline_controls, control_uids = partition_baseline_controls(baseline_rows, source)
    requested_population = [
        row
        for row in source
        if str(row.get("source_row_uid")) not in control_uids
        and (
            _bool(row.get("in_method_b_primary_population"))
            or (include_controls and _bool(row.get("method_b_control")))
        )
    ]
    selected_by_revision: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in requested_population:
        if row.get("revision_id") is not None:
            selected_by_revision[int(row["revision_id"])].append(row)
    revision_ids = sorted(selected_by_revision)
    if max_revisions is not None:
        revision_ids = revision_ids[:max_revisions]
    missing_revision_population = [
        row for row in requested_population if row.get("revision_id") is None
    ]
    selected_population = list(missing_revision_population)
    for revision_id in revision_ids:
        selected_population.extend(selected_by_revision[revision_id])
    selected_source_uids = {str(row["source_row_uid"]) for row in selected_population}

    # Attribution must see every frozen action sharing each selected revision,
    # including A-safe actions and non-sampled occurrences. Only requested rows
    # are emitted, but excluded actions still reserve plausible comments/hunks.
    attribution_source = _read_rows(paths.source_population)
    selected_revision_ids = set(revision_ids)
    attribution_context = [
        row
        for row in attribution_source
        if row.get("revision_id") is not None and int(row["revision_id"]) in selected_revision_ids
    ]
    represented_actions = {
        str(row["action_uid"]) for row in attribution_context if row.get("action_uid")
    }
    # WikiConv lifecycle actions without a WikiDisputes source occurrence still
    # participate as blockers in the revision-global assignment. They emit no
    # Method-B source row, but prevent a selected action from silently claiming
    # their changed comment or indivisible hunk.
    attribution_context.extend(
        row
        for row in _read_rows(_default_inputs(settings)["actions"])
        if row.get("revision_id") is not None
        and int(row["revision_id"]) in selected_revision_ids
        and str(row.get("action_uid")) not in represented_actions
    )
    by_revision: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in attribution_context:
        by_revision[int(row["revision_id"])].append(row)
    population_hash = _population_signature(
        selected_population, attribution_context=attribution_context
    )
    recovery_parameters = {
        "checkpoint_every": checkpoint_every,
        "max_revisions": max_revisions,
        "max_trace_cells": max_trace_cells,
        "include_controls": include_controls,
        "ambiguity_tolerance": ambiguity_tolerance,
        "max_assignment_actions": max_assignment_actions,
        "max_assignment_candidates": max_assignment_candidates,
        "max_assignment_edges": max_assignment_edges,
        "max_assignment_states": max_assignment_states,
    }
    recovery_config_hash = canonical_json_hash(recovery_parameters)

    pairs = {int(row["target_revision_id"]): row for row in _read_rows(paths.revision_pairs)}
    history_by_target: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if paths.revision_history.exists():
        for row in _read_rows(paths.revision_history):
            history_by_target[int(row["target_revision_id"])].append(row)
    required_ids = set(revision_ids)
    required_ids.update(
        int(pair["predecessor_revision_id"])
        for revision_id in revision_ids
        if (pair := pairs.get(revision_id)) and pair.get("predecessor_revision_id") not in (None, 0)
    )
    required_ids.update(
        int(row["ancestor_revision_id"])
        for rows in history_by_target.values()
        for row in rows
        if row.get("ancestor_revision_id") is not None
    )
    records = load_cached_revision_index(settings, paths.revision_index, required_ids=required_ids)
    checkpoint_root = (
        settings.roots.checkpoints
        / "revision_diff"
        / "recovery"
        / population_hash
        / recovery_config_hash
    )
    all_evidence: list[dict[str, Any]] = []
    for row in missing_revision_population:
        action = dict(row)
        action["source_occurrence_uids"] = [row.get("source_row_uid")]
        unavailable = evidence_as_rows(
            recover_revision_actions(
                [action],
                _unavailable_revision(None),
                _unavailable_revision(None),
            )
        )
        for evidence_row in unavailable:
            reasons = _json_list(evidence_row.get("reason_codes_json"))
            evidence_row["reason_codes_json"] = json.dumps(
                ["target_revision_metadata_missing", *reasons],
                ensure_ascii=False,
            )
        all_evidence.extend(unavailable)
    shards: list[dict[str, Any]] = []

    for start in range(0, len(revision_ids), checkpoint_every):
        batch_ids = revision_ids[start : start + checkpoint_every]
        shard = checkpoint_root / f"batch-{start // checkpoint_every:06d}.parquet"
        marker = shard.with_suffix(".json")
        if resume and shard.exists() and marker.exists():
            manifest = json.loads(marker.read_text(encoding="utf-8"))
            if (
                manifest.get("population_hash") == population_hash
                and manifest.get("workflow_version") == WORKFLOW_VERSION
                and manifest.get("recovery_config_hash") == recovery_config_hash
                and manifest.get("revision_ids") == batch_ids
            ):
                rows = _read_rows(shard)
                all_evidence.extend(rows)
                shards.append(manifest)
                continue
        batch_rows: list[dict[str, Any]] = []
        for revision_id in batch_ids:
            pair = pairs.get(revision_id)
            target_record = records.get(revision_id)
            predecessor_id = (
                int(pair["predecessor_revision_id"])
                if pair and pair.get("predecessor_revision_id") is not None
                else None
            )
            predecessor_record = records.get(predecessor_id or -1)
            target = (
                resolve_revision_text(settings, target_record)
                if target_record
                else _unavailable_revision(revision_id)
            )
            predecessor = (
                RevisionText.available("0", "")
                if predecessor_id == 0
                else resolve_revision_text(settings, predecessor_record)
                if predecessor_record
                else _unavailable_revision(predecessor_id)
            )
            history_hashes, history_complete = _history_body_hashes(
                settings, revision_id, history_by_target, records
            )
            actions = []
            for row in by_revision[revision_id]:
                action = dict(row)
                action["source_occurrence_uids"] = [row.get("source_row_uid")]
                action["action_count"] = len(by_revision[revision_id])
                action["restoration_history_body_hashes"] = history_hashes
                action["restoration_history_complete"] = history_complete
                action["parentid_verified"] = bool(pair and pair.get("parentid_verified"))
                action["revision_actor"] = target_record.actor_name_exact if target_record else None
                actions.append(action)
            evidence = recover_revision_actions(
                actions,
                predecessor,
                target,
                page_id=(
                    str(target_record.page_id) if target_record and target_record.page_id else None
                ),
                target_response_hash=(
                    target_record.response_content_sha256 if target_record else None
                ),
                predecessor_response_hash=(
                    predecessor_record.response_content_sha256 if predecessor_record else None
                ),
                target_content_pointer=(
                    target_record.response_blob_path if target_record else None
                ),
                predecessor_content_pointer=(
                    predecessor_record.response_blob_path if predecessor_record else None
                ),
                assignment_config=AssignmentConfig(
                    ambiguity_tolerance=ambiguity_tolerance,
                    max_actions_for_exhaustive_search=max_assignment_actions,
                    max_candidates_for_exhaustive_search=max_assignment_candidates,
                    max_edges_for_exhaustive_search=max_assignment_edges,
                    max_search_states=max_assignment_states,
                ),
                max_trace_cells=max_trace_cells,
            )
            batch_rows.extend(
                row
                for row in evidence_as_rows(evidence)
                if str(row["source_row_uid"]) in selected_source_uids
            )
        atomic_parquet(shard, table_from_union_pylist(batch_rows))
        manifest = {
            "workflow_version": WORKFLOW_VERSION,
            "population_hash": population_hash,
            "recovery_config_hash": recovery_config_hash,
            "recovery_parameters": recovery_parameters,
            "revision_ids": batch_ids,
            "rows": len(batch_rows),
            "artifact": file_descriptor(shard),
        }
        atomic_write_json(marker, manifest)
        all_evidence.extend(batch_rows)
        shards.append(manifest)

    all_evidence.sort(
        key=lambda row: (
            int(row.get("target_revision_id"))
            if str(row.get("target_revision_id")).isdigit()
            else -1,
            str(row.get("action_uid")),
            str(row.get("source_row_uid")),
        )
    )
    recovered_new_count = len(all_evidence)
    recovered_uids = [str(row["source_row_uid"]) for row in all_evidence]
    if len(recovered_uids) != len(set(recovered_uids)):
        raise RuntimeError("duplicate recovered source_row_uid in Method-B evidence")
    if set(recovered_uids) & control_uids:
        raise RuntimeError("immutable baseline control was emitted by Method-B recovery")
    # Preserve every baseline control field exactly as loaded, and append those
    # rows after the newly recovered evidence without merging source metadata.
    all_evidence.extend(baseline_controls)
    all_evidence.sort(
        key=lambda row: (
            int(row.get("target_revision_id"))
            if str(row.get("target_revision_id")).isdigit()
            else -1,
            str(row.get("action_uid")),
            str(row.get("source_row_uid")),
        )
    )
    atomic_parquet(evidence_path, table_from_union_pylist(all_evidence))
    representation_population = list(selected_population)
    representation_population.extend(
        source_row
        for source_row in source
        if str(source_row.get("source_row_uid")) in control_uids
    )
    population_by_source = {
        str(row["source_row_uid"]): row for row in representation_population
    }
    representations = _representations(all_evidence, population_by_source)
    atomic_parquet(representations_path, table_from_union_pylist(representations))

    report_rows = []
    for row in all_evidence:
        source_row = population_by_source[str(row["source_row_uid"])]
        report_rows.append(
            {
                **source_row,
                **row,
                "method_b_status": row["status"],
                "predecessor_available": row["predecessor_availability"]
                in {"available", "content_available", "exact_empty_root"},
                "deterministic_diff_available": bool(row.get("diff_operations_json") != "[]"),
                "recovered_markup_categories": _markup_categories(
                    _text(source_row.get("source_text")), _text(row.get("candidate_body"))
                ),
                "recovered_markup_additions": _markup_additions(
                    _text(source_row.get("source_text")), _text(row.get("candidate_body"))
                ),
            }
        )
    report = {
        "status": "bounded_recovery" if max_revisions is not None else "full_population_recovery",
        "workflow_version": WORKFLOW_VERSION,
        "population_hash": population_hash,
        "recovery_config_hash": recovery_config_hash,
        "recovery_parameters": recovery_parameters,
        "revision_count": len(revision_ids),
        "occurrences_without_target_revision": len(missing_revision_population),
        "evidence_rows": len(all_evidence),
        "reused_control_count": len(baseline_controls),
        "recovered_new_count": recovered_new_count,
        "immutable_baseline_evidence": baseline_descriptor,
        "checkpoint_shards": len(shards),
        "revision_context_occurrences": len(attribution_context),
        "recovery": recovery_report(report_rows),
        "artifacts": {
            "evidence": {**file_descriptor(evidence_path), "rows": len(all_evidence)},
            "representations": {
                **file_descriptor(representations_path),
                "rows": len(representations),
            },
        },
    }
    atomic_write_json(report_path, report)
    return report


def validate_pilot(settings: Settings, *, paths: MethodBPaths | None = None) -> dict[str, Any]:
    paths = paths or MethodBPaths.from_settings(settings)
    population = {str(row["source_row_uid"]): row for row in _read_rows(paths.pilot_population)}
    evidence = _read_rows(paths.pilot_recovery_evidence)
    rows: list[dict[str, Any]] = []
    for row in evidence:
        source = population.get(str(row["source_row_uid"]))
        if source is None:
            continue
        rows.append(
            {
                **source,
                **row,
                "method_b_candidate_raw_body": row.get("candidate_body"),
                "method_b_candidate_available": row.get("candidate_body") is not None,
                "method_b_left_boundary": row.get("candidate_start"),
                "method_b_right_boundary": row.get("candidate_end"),
                "method_b_contamination": row.get("neighboring_comment_contamination"),
                "method_b_assignment_ambiguity": bool(_json_list(row.get("ambiguity_flags_json"))),
            }
        )
    report = pilot_validation_report(rows)
    comparison_rows = report.pop("rows")
    atomic_parquet(paths.pilot_validation, table_from_union_pylist(comparison_rows))
    report.update(
        {
            "status": "automated_comparison_not_human_validation",
            "comparison_reference": "method_a_not_ground_truth",
            "comparison_rows": len(comparison_rows),
            "human_precision_recall": "not_calculated_no_adjudicated_labels",
            "artifact": {
                **file_descriptor(paths.pilot_validation),
                "rows": len(comparison_rows),
            },
        }
    )
    baseline_path = (
        paths.pilot_validation_report.parent
        / "diagnostic_pass"
        / "16_all_pilot_diagnostic_rows.csv"
    )
    with baseline_path.open("r", encoding="utf-8", newline="") as handle:
        baseline_rows = list(csv.DictReader(handle))
    comparison = localization_fix_comparison_report(
        baseline_rows,
        rows,
        validation_report=report,
        expected_rows=325,
        seed=20260818,
        per_stratum=25,
    )
    atomic_write_json(paths.localization_fix_comparison, comparison)
    report["localization_fix_comparison"] = file_descriptor(paths.localization_fix_comparison)
    archived_comparisons = _read_rows(paths.pre_boundary_usable_fix_validation)

    def enrich(
        comparisons: Sequence[Mapping[str, Any]], evidence_rows: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        evidence_by_uid = {str(row.get("source_row_uid", "")): row for row in evidence_rows}
        return [
            {**evidence_by_uid.get(str(row.get("entity_uid", "")), {}), **row}
            for row in comparisons
        ]

    archived_evidence = _read_rows(paths.pre_boundary_usable_fix_evidence)
    audit_uid_to_entity_uid = {
        str(row["audit_uid"]): str(row["entity_uid"])
        for row in _read_rows(paths.pre_boundary_usable_fix_audit_key)
        if row.get("audit_uid") not in (None, "") and row.get("entity_uid") not in (None, "")
    }
    boundary_usable_comparison = boundary_usable_fix_comparison_report(
        enrich(archived_comparisons, archived_evidence),
        enrich(comparison_rows, rows),
        audit_uid_to_entity_uid=audit_uid_to_entity_uid,
    )
    atomic_write_json(paths.boundary_usable_fix_comparison, boundary_usable_comparison)
    report["boundary_usable_fix_comparison"] = file_descriptor(paths.boundary_usable_fix_comparison)
    atomic_write_json(paths.pilot_validation_report, report)
    return report



def _method_b_selectable(method_b: Mapping[str, Any] | None) -> bool:
    """Return whether Method-B evidence is eligible for downstream selection."""
    if not method_b:
        return False

    status = _text(method_b.get("status"))
    if status not in METHOD_B_SELECTABLE_STATUSES:
        return False

    # Assignment ambiguity always fails closed, even if status plumbing
    # is malformed upstream.
    if _text(method_b.get("assignment_status")) == "ambiguous":
        return False
    if _json_list(method_b.get("ambiguity_flags_json")):
        return False

    # b_usable is valid only for the empirically approved soft reasons.
    if status == "b_usable":
        reasons = set(_json_list(method_b.get("reason_codes_json")))
        if not reasons or not reasons.issubset(SOFT_USABILITY_REASONS):
            return False

    # Safe/usable rows must actually contain a recoverable body.
    return method_b.get("candidate_body") is not None

def monotonic_selection_row(
    source: Mapping[str, Any], method_b: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Select A unchanged, then validated safe/usable B, else existing A text."""

    source_uid = str(source["source_row_uid"])
    method_a_status = _text(source.get("method_a_status"))
    method_a_text = _text(source.get("method_a_selected_text"))
    method_b_status = _text(method_b.get("status")) if method_b else "not_run"
    method_b_selectable = _method_b_selectable(method_b)
    if method_a_status == "promote":
        selected_method = "method_a"
        selected_text = method_a_text
        transition = "method_a_safe_unchanged"
    elif method_b_selectable:
        selected_method = "method_b"
        selected_text = _text(method_b.get("candidate_body"))
        transition = f"{method_a_status}_to_promote"
    else:
        selected_method = "method_a_fallback"
        selected_text = method_a_text
        transition = f"{method_a_status}_preserved"
    return {
        "source_row_uid": source_uid,
        "logical_utterance_uid": source.get("logical_utterance_uid"),
        "action_uid": source.get("action_uid"),
        "method_a_status": method_a_status,
        "method_a_selected_text_sha256": local_content_sha256(method_a_text),
        "method_b_status": method_b_status,
        "method_b_reason_codes_json": method_b.get("reason_codes_json") if method_b else "[]",
        "selected_method": selected_method,
        "transition": transition,
        "selected_text_sha256": local_content_sha256(selected_text),
        "selected_text": selected_text,
        "method_a_control_disagreement": method_a_status == "promote"
        and bool(method_b)
        and _text(method_b.get("candidate_body")) != method_a_text,
        "selection_version": WORKFLOW_VERSION,
    }


def select_combined(settings: Settings, *, paths: MethodBPaths | None = None) -> dict[str, Any]:
    """Apply monotonic A-then-B selection without changing downstream exports."""

    paths = paths or MethodBPaths.from_settings(settings)
    population = {str(row["source_row_uid"]): row for row in _read_rows(paths.source_population)}
    evidence_rows = _read_rows(paths.recovery_evidence)
    if paths.pilot_recovery_evidence.exists():
        pilot_controls = [
            row
            for row in _read_rows(paths.pilot_recovery_evidence)
            if population.get(str(row["source_row_uid"]), {}).get("method_a_status") == "promote"
        ]
        evidence_rows = merge_pilot_control_evidence(evidence_rows, pilot_controls)
    duplicate_b = [
        uid
        for uid, count in Counter(str(row["source_row_uid"]) for row in evidence_rows).items()
        if count > 1
    ]
    if duplicate_b:
        raise RuntimeError(f"duplicate Method-B evidence for source occurrences: {duplicate_b[:5]}")
    evidence = {str(row["source_row_uid"]): row for row in evidence_rows}
    audit: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    control_disagreements = 0
    for source_uid, source in sorted(population.items()):
        method_b = evidence.get(source_uid)
        selected = monotonic_selection_row(source, method_b)
        audit.append(selected)
        if selected["method_a_control_disagreement"]:
            control_disagreements += 1
        representations.append(
            {
                "source_row_uid": source_uid,
                "logical_utterance_uid": source.get("logical_utterance_uid"),
                "action_uid": source.get("action_uid"),
                "version_uid": source.get("version_uid"),
                "selected_method": selected["selected_method"],
                "selected_text": selected["selected_text"],
                "selected_text_sha256": selected["selected_text_sha256"],
                "selection_version": WORKFLOW_VERSION,
            }
        )
    unsafe_selected = [
        row["source_row_uid"]
        for row in audit
        if row["selected_method"] == "method_b"
        and not _method_b_selectable(evidence.get(str(row["source_row_uid"])))
    ]
    if unsafe_selected:
        raise RuntimeError(f"non-safe Method-B rows selected: {unsafe_selected[:5]}")
    a_immutability_failures = [
        row["source_row_uid"]
        for row in audit
        if row["method_a_status"] == "promote"
        and row["selected_text_sha256"] != row["method_a_selected_text_sha256"]
    ]
    if a_immutability_failures:
        raise RuntimeError(
            f"Method-A safe text changed during selection: {a_immutability_failures[:5]}"
        )
    atomic_parquet(paths.selection_audit, table_from_union_pylist(audit))
    atomic_parquet(paths.combined_representation, table_from_union_pylist(representations))
    counts = Counter(row["transition"] for row in audit)
    report = {
        "status": "selection_staged_downstream_exports_unchanged",
        "selection_version": WORKFLOW_VERSION,
        "rows": len(audit),
        "transition_counts": dict(sorted(counts.items())),
        "method_a_control_disagreements": control_disagreements,
        "method_a_safe_immutability_failures": 0,
        "unsafe_method_b_selected": 0,
        "backfill_accounting": recovery_report(
            [
                {
                    **population[source_uid],
                    **method_b,
                    "method_b_status": method_b.get("status"),
                    "predecessor_available": method_b.get("predecessor_availability")
                    in {"available", "content_available", "exact_empty_root"},
                    "deterministic_diff_available": method_b.get("diff_operations_json") != "[]",
                    "recovered_markup_categories": _markup_categories(
                        _text(population[source_uid].get("source_text")),
                        _text(method_b.get("candidate_body")),
                    ),
                    "recovered_markup_additions": _markup_additions(
                        _text(population[source_uid].get("source_text")),
                        _text(method_b.get("candidate_body")),
                    ),
                }
                for source_uid, method_b in sorted(evidence.items())
            ]
        ),
        "artifacts": {
            "audit": {**file_descriptor(paths.selection_audit), "rows": len(audit)},
            "combined": {
                **file_descriptor(paths.combined_representation),
                "rows": len(representations),
            },
        },
    }
    atomic_write_json(paths.selection_report, report)
    return report


def build_human_audit(
    settings: Settings,
    *,
    paths: MethodBPaths | None = None,
    seed: int = 20260818,
    excerpt_limit: int = 500,
    per_stratum: int = 25,
    pilot: bool = False,
) -> dict[str, Any]:
    paths = paths or MethodBPaths.from_settings(settings)
    population_path = paths.pilot_population if pilot else paths.source_population
    evidence_path = paths.pilot_recovery_evidence if pilot else paths.recovery_evidence
    packet_path = paths.pilot_audit_packet if pilot else paths.audit_packet
    key_path = paths.pilot_audit_key if pilot else paths.audit_key
    manifest_path = paths.pilot_audit_manifest if pilot else paths.audit_manifest
    population = {str(row["source_row_uid"]): row for row in _read_rows(population_path)}
    evidence = _read_rows(evidence_path)
    if not pilot and paths.pilot_recovery_evidence.exists():
        evidence.extend(
            row
            for row in _read_rows(paths.pilot_recovery_evidence)
            if population.get(str(row["source_row_uid"]), {}).get("method_a_status") == "promote"
        )
    required_ids: set[int] = set()
    for row in evidence:
        target_id = _int_or_none(row.get("target_revision_id"))
        predecessor_id = _int_or_none(row.get("predecessor_revision_id"))
        if target_id is not None:
            required_ids.add(target_id)
        if predecessor_id not in (None, 0):
            required_ids.add(predecessor_id)
    records = load_cached_revision_index(settings, paths.revision_index, required_ids=required_ids)
    rows: list[dict[str, Any]] = []
    for row in evidence:
        source = population[str(row["source_row_uid"])]
        target_id = _int_or_none(row.get("target_revision_id"))
        target_record = records.get(target_id) if target_id is not None else None
        predecessor_id = _int_or_none(row.get("predecessor_revision_id"))
        predecessor_record = records.get(predecessor_id or -1)
        target = resolve_revision_text(settings, target_record) if target_record else None
        predecessor = (
            RevisionText.available("0", "")
            if predecessor_id == 0
            else resolve_revision_text(settings, predecessor_record)
            if predecessor_record
            else None
        )
        target_ranges = json.loads(row.get("target_changed_ranges_json") or "[]")
        predecessor_ranges = json.loads(row.get("predecessor_changed_ranges_json") or "[]")
        target_span = target_ranges[0] if target_ranges else (None, None)
        predecessor_span = predecessor_ranges[0] if predecessor_ranges else (None, None)
        rows.append(
            {
                **source,
                **row,
                "method_b_status": row.get("status"),
                "method_b_candidate_raw_body": row.get("candidate_body"),
                "target_wikitext": target.raw_text if target else None,
                "predecessor_wikitext": predecessor.raw_text if predecessor else None,
                "target_changed_start": target_span[0],
                "target_changed_end": target_span[1],
                "predecessor_changed_start": predecessor_span[0],
                "predecessor_changed_end": predecessor_span[1],
                "changed_ranges_json": row.get("target_changed_ranges_json"),
            }
        )
    selected = select_stratified_pilot(rows, seed=seed, per_stratum=per_stratum)
    selected_rows = selected.pop("rows")
    audit = blinded_audit_packet(selected_rows, seed=seed, excerpt_limit=excerpt_limit)
    reviewer_rows = audit.pop("reviewer_rows")
    key_rows = audit.pop("unblinding_key")
    atomic_parquet(packet_path, table_from_union_pylist(reviewer_rows))
    atomic_parquet(key_path, table_from_union_pylist(key_rows))
    manifest = {
        **audit,
        "status": "blinded_unadjudicated",
        "population_scope": "pilot" if pilot else "full_with_pilot_controls",
        "reviewer_rows": len(reviewer_rows),
        "key_rows": len(key_rows),
        "sampling": selected,
        "precision_recall": "not_calculated_no_adjudicated_labels",
        "artifacts": {
            "reviewer_packet": file_descriptor(packet_path),
            "unblinding_key": file_descriptor(key_path),
        },
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def _accepted_validation(path: Path) -> dict[str, Any]:
    decision = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decision, dict) or decision.get("method_b_accepted") is not True:
        raise RuntimeError(
            "Stage 6 requires a separate validation decision with method_b_accepted=true"
        )
    if not decision.get("adjudicated_by") or not decision.get("adjudicated_at"):
        raise RuntimeError("validation decision requires adjudicated_by and adjudicated_at")
    return decision


def rebuild_annotation_export(
    settings: Settings,
    *,
    validation_decision: Path,
    paths: MethodBPaths | None = None,
    input_csv: Path | None = None,
) -> dict[str, Any]:
    """Explicit Stage-6 rebuild to a new file; current exports remain untouched."""

    paths = paths or MethodBPaths.from_settings(settings)
    decision = _accepted_validation(validation_decision)
    source_path = input_csv or (
        settings.roots.output / "annotation" / "wikidisputes_llm_annotation_input.csv"
    )
    selected = {
        str(row["source_row_uid"]): row for row in _read_rows(paths.combined_representation)
    }
    output = io.StringIO(newline="")
    with source_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise RuntimeError("annotation input has no header")
        writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator="\n")
        writer.writeheader()
        rows = 0
        method_b_rows = 0
        for row in reader:
            rows += 1
            source_uid = row.get("ssot_source_row_uid", "")
            selection = selected.get(source_uid)
            if selection and selection.get("selected_method") == "method_b":
                row["utterance_text"] = _text(selection.get("selected_text"))
                if "ssot_annotation_text_source" in row:
                    row["ssot_annotation_text_source"] = (
                        "mediawiki_revision_diff_comment_wikitext_body"
                    )
                method_b_rows += 1
            writer.writerow(row)
    atomic_write_bytes(paths.staged_annotation, output.getvalue().encode("utf-8"))
    report = {
        "status": "explicit_validated_stage_6_rebuild",
        "validation_decision": decision,
        "input": file_descriptor(source_path),
        "output": file_descriptor(paths.staged_annotation),
        "rows": rows,
        "method_b_rows": method_b_rows,
        "current_annotation_export_overwritten": False,
    }
    atomic_write_json(paths.staged_annotation.with_suffix(".json"), report)
    return report


def final_invariants(
    settings: Settings,
    *,
    paths: MethodBPaths | None = None,
    staged_annotation: Path | None = None,
) -> dict[str, Any]:
    paths = paths or MethodBPaths.from_settings(settings)
    population = _read_rows(paths.source_population)
    selection = _read_rows(paths.selection_audit)
    join = _read_rows(_default_inputs(settings)["join"])
    evidence = {str(row["source_row_uid"]): row for row in _read_rows(paths.recovery_evidence)}
    checks = {
        "canonical_join_source_rows_unique": len(join)
        == len({str(row["source_row_uid"]) for row in join}),
        "substantive_population_matches_method_a_audit": len(population)
        == len(_read_rows(_default_inputs(settings)["method_a_audit"])),
        "source_occurrence_identities_unchanged": {str(row["source_row_uid"]) for row in population}
        == {str(row["source_row_uid"]) for row in selection},
        "zero_canonical_source_provenance_mismatch": all(
            _bool(row.get("source_provenance_exact")) for row in population
        ),
        "zero_outcome_leakage": not any(
            any("outcome" in str(key).casefold() for key in row) for row in selection
        ),
        "method_a_safe_byte_identical": all(
            row["selected_text_sha256"] == row["method_a_selected_text_sha256"]
            for row in selection
            if row["method_a_status"] == "promote"
        ),
        "no_non_safe_method_b_selected": all(
            _method_b_selectable(evidence.get(str(row["source_row_uid"])))
            for row in selection
            if row["selected_method"] == "method_b"
        ),
    }
    annotation_path = staged_annotation or paths.staged_annotation
    if not annotation_path.exists():
        raise FileNotFoundError(
            f"Stage 7 requires the explicit Stage-6 artifact: {annotation_path}"
        )
    immutable_annotation_fields_unchanged: bool | None = None
    base = settings.roots.output / "annotation" / "wikidisputes_llm_annotation_input.csv"
    with (
        base.open("r", encoding="utf-8", newline="") as left,
        annotation_path.open("r", encoding="utf-8", newline="") as right,
    ):
        left_rows = list(csv.DictReader(left))
        right_rows = list(csv.DictReader(right))
    mutable = {"utterance_text", "ssot_annotation_text_source"}
    immutable_annotation_fields_unchanged = len(left_rows) == len(right_rows) and all(
        {key: value for key, value in first.items() if key not in mutable}
        == {key: value for key, value in second.items() if key not in mutable}
        for first, second in zip(left_rows, right_rows, strict=True)
    )
    checks["ids_order_chronology_reply_structure_outcomes_unchanged"] = bool(
        immutable_annotation_fields_unchanged
    )
    status = "pass" if all(value is True for value in checks.values()) else "fail"
    report = {
        "status": status,
        "workflow_version": WORKFLOW_VERSION,
        "local_authoritative_counts": {
            "source_rows": len(join),
            "substantive_occurrences": len(population),
            "context_rows": sum(row.get("context_node_uid") is not None for row in join),
            "logical_utterances": len({row.get("logical_utterance_uid") for row in population}),
        },
        "checks": checks,
        "immutable_annotation_fields_unchanged": immutable_annotation_fields_unchanged,
    }
    atomic_write_json(paths.invariants_report, report)
    if status != "pass":
        raise RuntimeError(f"Method-B final invariants failed: {checks}")
    return report
