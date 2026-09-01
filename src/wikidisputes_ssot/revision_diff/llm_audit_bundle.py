"""Generate a self-contained, frozen-sample LLM audit bundle."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
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

from .boundaries import extract_comment_candidates
from .cache import load_cached_revision_index, resolve_revision_text
from .llm_audit_controls import (
    select_calibration_controls,
    unavailable_taxonomy,
    validate_calibration_key,
)
from .llm_audit_windows import format_review_object
from .residual_ceiling import SEED
from .residual_ceiling_workflow import ResidualCeilingPaths
from .workflow import MethodBPaths

BUNDLE_VERSION = "llm-residual-audit-bundle-v1"
SAMPLE_COUNT = 600
UNAVAILABLE_COUNT = 818
CONTROL_COUNT = 50
DESIGN_FIELDS = (
    "source_row_uid",
    "review_order",
    "primary_stratum",
    "design_cell",
    "diagnostic_domain",
    "population_n",
    "sample_n",
    "inclusion_probability",
    "survey_weight",
)
FROZEN_OUTPUT_NAMES = (
    "method_b_recovery_evidence.parquet",
    "method_b_representations.parquet",
    "method_b_recovery_report.json",
    "method_b_selection_audit.parquet",
    "method_b_combined_representation.parquet",
    "method_b_selection_report.json",
)


@dataclass(frozen=True, slots=True)
class LLMAuditBundlePaths:
    root: Path
    sample_evidence: Path
    sample_review: Path
    unavailable: Path
    calibration_controls: Path
    calibration_key: Path
    manifest: Path

    @classmethod
    def from_settings(cls, settings: Settings, seed: str = SEED) -> LLMAuditBundlePaths:
        audit = ResidualCeilingPaths.from_settings(settings, seed)
        root = audit.root / "llm_audit_bundle"
        return cls(
            root=root,
            sample_evidence=root / "sample_evidence.parquet",
            sample_review=root / "sample_review.jsonl",
            unavailable=root / "unavailable_818.parquet",
            calibration_controls=root / "calibration_controls.jsonl",
            calibration_key=root / "calibration_key.parquet",
            manifest=root / "bundle_manifest.json",
        )


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    return pq.read_table(path).to_pylist()


def _read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    csv.field_size_limit(sys.maxsize)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _prefixed(row: dict[str, Any] | None, prefix: str) -> dict[str, Any]:
    return {} if row is None else {f"{prefix}__{key}": value for key, value in row.items()}


def _hash_rank(seed: str, uid: str) -> str:
    return hashlib.sha256(f"{seed}:{uid}".encode()).hexdigest()


def _file_hashes(paths: list[Path]) -> dict[str, str]:
    return {str(path): file_descriptor(path)["sha256"] for path in paths}


def _frozen_output_paths(paths: MethodBPaths) -> list[Path]:
    return [
        paths.recovery_evidence,
        paths.representations,
        paths.recovery_report,
        paths.selection_audit,
        paths.combined_representation,
        paths.selection_report,
    ]


def _raw_revision_map(
    settings: Settings, paths: MethodBPaths, revision_ids: set[int]
) -> tuple[dict[int, str | None], dict[int, dict[str, Any]]]:
    records = load_cached_revision_index(settings, paths.revision_index, required_ids=revision_ids)
    raw: dict[int, str | None] = {0: ""}
    states: dict[int, dict[str, Any]] = {
        0: {"availability_status": "exact_empty_root", "resolution": "available"}
    }
    for revision_id in sorted(revision_ids):
        record = records.get(revision_id)
        if record is None:
            raw[revision_id] = None
            states[revision_id] = {
                "availability_status": "no_index_record",
                "resolution": "unavailable",
            }
            continue
        state = record.to_row()
        try:
            resolved = resolve_revision_text(settings, record)
        except Exception as error:  # Exact error text is audit evidence.
            raw[revision_id] = None
            state.update(
                {
                    "resolution": "error",
                    "resolution_error_type": type(error).__name__,
                    "resolution_error": str(error),
                }
            )
        else:
            raw[revision_id] = resolved.raw_text
            state["resolution"] = "available" if resolved.raw_text is not None else "unavailable"
            state["resolved_availability"] = str(resolved.availability)
        states[revision_id] = state
    return raw, states


def _source_matches(raw: str | None, source_text: Any) -> list[dict[str, int]]:
    source = "" if source_text is None else str(source_text)
    if raw is None or not source:
        return []
    return [
        {"start": match.start(), "end": match.end()}
        for match in re.finditer(re.escape(source), raw)
    ]


def _candidate_dicts(raw: str | None) -> list[dict[str, Any]]:
    if raw is None:
        return []
    candidates = []
    for candidate in extract_comment_candidates(raw):
        value = asdict(candidate)
        value["boundary_evidence"] = list(value["boundary_evidence"])
        value["boundary_warnings"] = list(value["boundary_warnings"])
        if raw[value["start"] : value["end"]] != value["raw_wikitext"]:
            raise RuntimeError("structural candidate raw interval mismatch")
        if raw[value["body_start"] : value["body_end"]] != value["body_wikitext"]:
            raise RuntimeError("structural candidate body interval mismatch")
        candidates.append(value)
    return candidates


def _range_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _changed_ranges(recovery: dict[str, Any] | None, raw_length: int) -> list[tuple[int, int]]:
    if recovery is None:
        return []
    values = _json_list(
        recovery.get("action_target_changed_ranges_json")
        or recovery.get("target_changed_ranges_json")
    )
    output = []
    for value in values:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            start, end = _integer(value[0]), _integer(value[1])
            if start is not None and end is not None and 0 <= start < end <= raw_length:
                output.append((start, end))
    return output


def _competing_candidates(
    recovery: dict[str, Any] | None, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if recovery is None:
        return []
    requested = {str(value) for value in _json_list(recovery.get("competing_candidates_json"))}
    return [candidate for candidate in candidates if candidate["candidate_uid"] in requested]


def _focal_candidates(
    recovery: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    source_matches: list[dict[str, int]],
    changed: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    chosen: set[str] = set()
    competing = _competing_candidates(recovery, candidates)
    # All competing candidates remain explicit evidence, but only a bounded
    # subset drives surrounding raw windows on whole-page ambiguity cases.
    chosen.update(item["candidate_uid"] for item in competing[:5])
    if recovery is not None:
        start, end = (
            _integer(recovery.get("candidate_start")),
            _integer(recovery.get("candidate_end")),
        )
        if start is not None and end is not None:
            chosen.update(
                item["candidate_uid"]
                for item in candidates
                if item["start"] == start and item["end"] == end
            )
    focal_ranges = [(item["start"], item["end"]) for item in source_matches] + changed
    for focal in focal_ranges:
        overlaps = [
            item for item in candidates if _range_overlap((item["start"], item["end"]), focal)
        ]
        if overlaps:
            chosen.update(item["candidate_uid"] for item in overlaps[:3])
        elif candidates:
            nearest = min(
                candidates,
                key=lambda item: min(abs(item["start"] - focal[1]), abs(focal[0] - item["end"])),
            )
            chosen.add(nearest["candidate_uid"])
    return [item for item in candidates if item["candidate_uid"] in chosen]


def _competing_actions(
    recovery: dict[str, Any] | None, recovery_by_action: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    if recovery is None:
        return []
    output = []
    for action_uid in _json_list(recovery.get("competing_actions_json")):
        action = recovery_by_action.get(str(action_uid))
        if action is not None:
            output.append(
                {
                    key: action.get(key)
                    for key in (
                        "source_row_uid",
                        "action_uid",
                        "action_type",
                        "action_offset_hint",
                        "action_target_changed_ranges_json",
                        "assignment_status",
                        "assignment_evidence_json",
                        "reason_codes_json",
                    )
                }
            )
    return output


def _diff_review_evidence(recovery: dict[str, Any] | None) -> dict[str, Any]:
    recovery = recovery or {}
    operations = _json_list(recovery.get("diff_operations_json"))
    kinds = Counter(
        str(operation.get("kind") or "unobserved")
        for operation in operations
        if isinstance(operation, dict)
    )
    return {
        "operation_count": len(operations),
        "operation_kinds": dict(sorted(kinds.items())),
        "target_changed_ranges": _json_list(recovery.get("target_changed_ranges_json")),
        "predecessor_changed_ranges": _json_list(recovery.get("predecessor_changed_ranges_json")),
        "action_target_changed_ranges": _json_list(
            recovery.get("action_target_changed_ranges_json")
        ),
        "hunk_attribution": _json_list(recovery.get("hunk_attribution_evidence_json")),
        "localization": _json_list(recovery.get("localization_evidence_json")),
    }


def _discussiontools_review_evidence(
    discussiontools: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if discussiontools is None:
        return None
    return {
        key: discussiontools.get(key)
        for key in (
            "discussiontools_state_status",
            "parser_success",
            "exact_boundary_agreement",
            "contamination_status",
            "proposed_safe",
            "failure_reasons",
            "discussiontools_payload_json",
            "discussiontools_error_json",
        )
    }


def _validate_advertised_candidates(row: dict[str, Any]) -> int:
    raw = row.get("target_wikitext")
    if raw is None:
        return 0
    checked = 0
    recovery = row.get("_recovery")
    if recovery:
        start, end = (
            _integer(recovery.get("candidate_start")),
            _integer(recovery.get("candidate_end")),
        )
        supplied = recovery.get("candidate_raw")
        if start is not None and end is not None and supplied is not None:
            if not 0 <= start < end <= len(raw) or raw[start:end] != supplied:
                raise RuntimeError(f"recovery candidate interval mismatch: {row['source_row_uid']}")
            checked += 1
    source = row.get("_source") or {}
    start, end = (
        _integer(source.get("method_a_left_boundary")),
        _integer(source.get("method_a_right_boundary")),
    )
    supplied = source.get("method_a_candidate_full_raw")
    if start is not None and end is not None and supplied not in (None, ""):
        if not 0 <= start < end <= len(raw) or raw[start:end] != supplied:
            raise RuntimeError(f"Method-A candidate interval mismatch: {row['source_row_uid']}")
        checked += 1
    return checked


def _joined_row(
    uid: str,
    *,
    source: dict[str, Any],
    recovery: dict[str, Any] | None,
    selection: dict[str, Any],
    discussiontools: dict[str, Any] | None,
    audit: dict[str, str] | None,
    raw: dict[int, str | None],
    raw_states: dict[int, dict[str, Any]],
    recovery_by_action: dict[str, dict[str, Any]],
    candidate_cache: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    target_id = _integer((recovery or {}).get("target_revision_id") or source.get("revision_id"))
    predecessor_id = _integer(
        (recovery or {}).get("predecessor_revision_id") or source.get("predecessor_revision_id")
    )
    target_raw = raw.get(target_id) if target_id is not None else None
    predecessor_raw = raw.get(predecessor_id) if predecessor_id is not None else None
    candidate_key = target_id or -1
    if candidate_key not in candidate_cache:
        candidate_cache[candidate_key] = _candidate_dicts(target_raw)
    candidates = candidate_cache[candidate_key]
    matches = _source_matches(target_raw, source.get("source_text"))
    changed = _changed_ranges(recovery, len(target_raw or ""))
    competing_candidates = _competing_candidates(recovery, candidates)
    focal_candidates = _focal_candidates(recovery, candidates, matches, changed)
    row: dict[str, Any] = {
        **(audit or {}),
        "source_row_uid": uid,
        **_prefixed(source, "source"),
        **_prefixed(recovery, "recovery"),
        **_prefixed(selection, "selection"),
        **_prefixed(discussiontools, "discussiontools"),
        "target_revision_id": target_id,
        "predecessor_revision_id": predecessor_id,
        "source_text": source.get("source_text"),
        "target_wikitext": target_raw,
        "predecessor_wikitext": predecessor_raw,
        "target_cache_state_json": json.dumps(
            raw_states.get(target_id or -1, {}), ensure_ascii=False, sort_keys=True
        ),
        "predecessor_cache_state_json": json.dumps(
            raw_states.get(predecessor_id or -1, {}), ensure_ascii=False, sort_keys=True
        ),
        "all_candidates": candidates,
        "focal_candidates": focal_candidates,
        "competing_candidate_evidence": competing_candidates,
        "competing_action_evidence": _competing_actions(recovery, recovery_by_action),
        "source_match_spans": matches,
        "current_failure_reasons": _json_list((recovery or {}).get("reason_codes_json")),
        "current_status": (recovery or {}).get("status"),
        "failure_reasons": _json_list((recovery or {}).get("reason_codes_json")),
        "reason_codes_json": (recovery or {}).get("reason_codes_json"),
        "status": (recovery or {}).get("status"),
        "lifecycle": source.get("action_type"),
        "target_changed_ranges_json": (recovery or {}).get("target_changed_ranges_json"),
        "predecessor_changed_ranges_json": (recovery or {}).get("predecessor_changed_ranges_json"),
        "action_target_changed_ranges_json": (recovery or {}).get(
            "action_target_changed_ranges_json"
        ),
        "candidate_start": (recovery or {}).get("candidate_start"),
        "candidate_end": (recovery or {}).get("candidate_end"),
        "candidate_raw": (recovery or {}).get("candidate_raw"),
        "candidate_body": (recovery or {}).get("candidate_body"),
        "competing_candidates_json": (recovery or {}).get("competing_candidates_json"),
        "competing_actions_json": (recovery or {}).get("competing_actions_json"),
        "assignment_evidence_json": (recovery or {}).get("assignment_evidence_json"),
        "lifecycle_consistency": (recovery or {}).get("lifecycle_consistency"),
        "signature_status": (recovery or {}).get("signature_status"),
        "signature_raw": (recovery or {}).get("signature_raw"),
        "signature_author": (recovery or {}).get("signature_author"),
        "revision_actor": (recovery or {}).get("revision_actor") or source.get("revision_actor"),
        "wikiconv_speaker": (recovery or {}).get("wikiconv_speaker")
        or source.get("wikiconv_speaker"),
        "discussiontools_evidence": discussiontools is not None,
        "discussiontools_review_evidence": _discussiontools_review_evidence(discussiontools),
        "token_persistence_evidence": (
            (recovery or {}).get("predecessor_target_continuity") == "token_persistence_continuity"
        ),
        "diff_span_evidence_json": json.dumps(
            _diff_review_evidence(recovery), ensure_ascii=False, sort_keys=True
        ),
        "_source": source,
        "_recovery": recovery,
    }
    _validate_advertised_candidates(row)
    row.pop("_source")
    row.pop("_recovery")
    return row


def _review_projection(row: dict[str, Any]) -> dict[str, Any]:
    """Drop full joined columns while retaining compact, focal review evidence."""

    visible = {
        key: value
        for key, value in row.items()
        if not key.startswith(("source__", "recovery__", "selection__", "discussiontools__"))
    }
    visible["all_candidates"] = row.get("focal_candidates") or []
    visible["diff_span_evidence"] = json.loads(row["diff_span_evidence_json"])
    visible.pop("diff_span_evidence_json", None)
    return visible


def _signature_state(source: dict[str, Any], recovery: dict[str, Any] | None) -> str:
    raw = str(source.get("method_a_candidate_full_raw") or "")
    if re.search(r"preceding unsigned comment|unsigned comment added by", raw, re.I):
        return "autosigned"
    if (recovery or {}).get("signature_status") == "explicit_evidence_observed":
        return "signed"
    if re.search(r"\b(?:19|20)\d{2}\b", raw) and re.search(
        r"\[\[(?:User|Special\s*:\s*Contributions)", raw, re.I
    ):
        return "signed"
    return "unsigned"


def _control_tier(
    source: dict[str, Any], recovery: dict[str, Any] | None, selection: dict[str, Any]
) -> str | None:
    if (
        source.get("method_a_status") == "promote"
        and selection.get("selected_method") == "method_a"
    ):
        return "a_safe_raw_promotion"
    status = (recovery or {}).get("status")
    if selection.get("selected_method") == "method_b" and status in {"b_safe", "b_usable"}:
        return str(status)
    return None


def _precontrol_uids(
    source: dict[str, dict[str, Any]],
    recovery: dict[str, dict[str, Any]],
    selection: dict[str, dict[str, Any]],
    frozen: set[str],
    *,
    seed: str,
) -> list[str]:
    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    autosigned: list[str] = []
    for uid, source_row in source.items():
        if uid in frozen:
            continue
        tier = _control_tier(source_row, recovery.get(uid), selection[uid])
        lifecycle = str(source_row.get("action_type") or "unobserved")
        if tier is None:
            continue
        cells[(tier, lifecycle)].append(uid)
        if _signature_state(source_row, recovery.get(uid)) == "autosigned":
            autosigned.append(uid)
    selected: set[str] = set(autosigned)
    for members in cells.values():
        selected.update(sorted(members, key=lambda uid: (_hash_rank(seed, uid), uid))[:40])
    return sorted(selected)


def _control_input(
    row: dict[str, Any],
    source: dict[str, Any],
    recovery: dict[str, Any] | None,
    selection: dict[str, Any],
) -> dict[str, Any] | None:
    tier = _control_tier(source, recovery, selection)
    if tier is None or row.get("target_wikitext") is None:
        return None
    if tier == "a_safe_raw_promotion":
        start = _integer(source.get("method_a_left_boundary"))
        end = _integer(source.get("method_a_right_boundary"))
        accepted_raw = source.get("method_a_candidate_full_raw")
        accepted_body = source.get("method_a_candidate_raw_body")
        provenance = "method_a_historical_raw_high_confidence"
    else:
        start = _integer((recovery or {}).get("candidate_start"))
        end = _integer((recovery or {}).get("candidate_end"))
        accepted_raw = (recovery or {}).get("candidate_raw")
        accepted_body = (recovery or {}).get("candidate_body")
        provenance = "method_b_exact_revision_diff"
    raw = row["target_wikitext"]
    if (
        start is None
        or end is None
        or accepted_raw is None
        or not 0 <= start < end <= len(raw)
        or raw[start:end] != accepted_raw
    ):
        return None
    output = {
        **row,
        "selected_method": selection.get("selected_method"),
        "status": source.get("method_a_status"),
        "method_b_status": (recovery or {}).get("status")
        if tier != "a_safe_raw_promotion"
        else None,
        "accepted_start": start,
        "accepted_end": end,
        "accepted_raw": accepted_raw,
        "accepted_body": accepted_body,
        "accepted_provenance": provenance,
        "accepted_tier": tier,
        "signature_state": _signature_state(source, recovery),
        # Expose the accepted interval as an ordinary candidate, without its
        # acceptance label, so every calibration row has offset-safe evidence.
        "candidate_start": start,
        "candidate_end": end,
        "candidate_raw": accepted_raw,
        "candidate_body": accepted_body,
    }
    return output


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return (
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        )
    ).encode("utf-8")


def _validate_review_rows(
    evidence_rows: list[dict[str, Any]], review_rows: list[dict[str, Any]]
) -> dict[str, int]:
    if len(evidence_rows) != len(review_rows):
        raise RuntimeError("review/evidence row count mismatch")
    windows_checked = focal_checked = 0
    for evidence, review in zip(evidence_rows, review_rows, strict=True):
        target = evidence.get("target_wikitext") or ""
        predecessor = evidence.get("predecessor_wikitext") or ""
        target_windows = review.get("target_windows") or []
        predecessor_windows = review.get("predecessor_windows") or []
        for raw, windows in ((target, target_windows), (predecessor, predecessor_windows)):
            for window in windows:
                start, end = int(window["start"]), int(window["end"])
                if not 0 <= start < end <= len(raw) or raw[start:end] != window["raw_text"]:
                    raise RuntimeError("review raw-window offset mismatch")
                windows_checked += 1
        focal_ranges: list[tuple[int, int]] = []
        focal_ranges.extend(
            (int(item["start"]), int(item["end"]))
            for item in evidence.get("focal_candidates") or []
        )
        focal_ranges.extend(
            (int(item["start"]), int(item["end"]))
            for item in evidence.get("source_match_spans") or []
        )
        primary_start, primary_end = (
            _integer(evidence.get("candidate_start")),
            _integer(evidence.get("candidate_end")),
        )
        if primary_start is not None and primary_end is not None:
            focal_ranges.append((primary_start, primary_end))
        for start, end in _changed_ranges_from_row(evidence, len(target)):
            if end - start <= 480:
                focal_ranges.append((start, end))
            else:
                focal_ranges.extend(((start, start + 240), (end - 240, end)))
        for start, end in focal_ranges:
            if not any(
                int(window["start"]) <= start and end <= int(window["end"])
                for window in target_windows
            ):
                raise RuntimeError("focal evidence is not fully contained in a raw window")
            focal_checked += 1
    return {"raw_windows_checked": windows_checked, "focal_ranges_checked": focal_checked}


def _changed_ranges_from_row(row: dict[str, Any], raw_length: int) -> list[tuple[int, int]]:
    recovery = {
        "action_target_changed_ranges_json": row.get("action_target_changed_ranges_json"),
        "target_changed_ranges_json": row.get("target_changed_ranges_json"),
    }
    return _changed_ranges(recovery, raw_length)


def _missingness(rows: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "source_text": sum(not bool(row.get("source_text")) for row in rows),
        "target_raw_revision": sum(row.get("target_wikitext") is None for row in rows),
        "predecessor_raw_revision": sum(row.get("predecessor_wikitext") is None for row in rows),
        "candidates": sum(not bool(row.get("all_candidates")) for row in rows),
        "localizable_source_match": sum(not bool(row.get("source_match_spans")) for row in rows),
        "usable_focal_window": sum(not bool(review.get("target_windows")) for review in reviews),
    }


def _schema_text(table: pa.Table) -> str:
    return str(table.schema)


def _git_commit(repository_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_llm_audit_bundle(
    settings: Settings,
    *,
    seed: str = SEED,
) -> dict[str, Any]:
    """Generate all bundle artifacts from the existing frozen audit sample."""

    method_paths = MethodBPaths.from_settings(settings)
    audit_paths = ResidualCeilingPaths.from_settings(settings, seed)
    bundle_paths = LLMAuditBundlePaths.from_settings(settings, seed)
    repository_root = Path(__file__).resolve().parents[3]
    frozen_paths = _frozen_output_paths(method_paths)
    frozen_before = _file_hashes(frozen_paths)

    audit_rows, audit_columns = _read_csv(audit_paths.csv)
    audit_manifest = json.loads(audit_paths.manifest.read_text(encoding="utf-8"))
    if len(audit_rows) != SAMPLE_COUNT:
        raise RuntimeError(f"frozen audit must have exactly {SAMPLE_COUNT} rows")
    sample_uids = [row["source_row_uid"] for row in audit_rows]
    if len(set(sample_uids)) != SAMPLE_COUNT:
        raise RuntimeError("frozen audit sample UIDs are not unique")
    sample_uid_hash = canonical_json_hash(sample_uids)
    if sample_uid_hash != audit_manifest["sample_source_uid_sha256"]:
        raise RuntimeError("frozen sample UID/order hash mismatch")
    sample_design = [{field: row[field] for field in DESIGN_FIELDS} for row in audit_rows]
    sample_design_hash = canonical_json_hash(sample_design)

    source_rows = _read_parquet(method_paths.source_population)
    recovery_rows = _read_parquet(method_paths.recovery_evidence)
    selection_rows = _read_parquet(method_paths.selection_audit)
    discussiontools_path = (
        method_paths.source_population.parent / "discussiontools_feasibility_evidence.parquet"
    )
    discussiontools_rows = (
        _read_parquet(discussiontools_path) if discussiontools_path.exists() else []
    )
    source = {str(row["source_row_uid"]): row for row in source_rows}
    recovery = {str(row["source_row_uid"]): row for row in recovery_rows}
    selection = {str(row["source_row_uid"]): row for row in selection_rows}
    discussiontools = {str(row["source_row_uid"]): row for row in discussiontools_rows}
    recovery_by_action = {
        str(row["action_uid"]): row
        for row in recovery_rows
        if row.get("action_uid") not in (None, "")
    }
    if set(source) != set(selection):
        raise RuntimeError("source and selection UID sets differ")

    unavailable_uids = [
        uid
        for uid, row in selection.items()
        if row.get("selected_method") == "method_a_fallback"
        and row.get("method_b_status") == "b_unavailable"
    ]
    if len(unavailable_uids) != UNAVAILABLE_COUNT or len(set(unavailable_uids)) != len(
        unavailable_uids
    ):
        raise RuntimeError("current b_unavailable population is not exactly 818 unique rows")
    if set(unavailable_uids) & set(sample_uids):
        raise RuntimeError("b_unavailable rows leaked into the frozen human sample")

    precontrol_uids = _precontrol_uids(source, recovery, selection, set(sample_uids), seed=seed)
    relevant_uids = set(sample_uids) | set(unavailable_uids) | set(precontrol_uids)
    revision_ids: set[int] = set()
    for uid in relevant_uids:
        source_row, recovery_row = source[uid], recovery.get(uid)
        for value in (
            (recovery_row or {}).get("target_revision_id") or source_row.get("revision_id"),
            (recovery_row or {}).get("predecessor_revision_id")
            or source_row.get("predecessor_revision_id"),
        ):
            revision_id = _integer(value)
            if revision_id is not None:
                revision_ids.add(revision_id)
    raw, raw_states = _raw_revision_map(settings, method_paths, revision_ids)
    candidate_cache: dict[int, list[dict[str, Any]]] = {}

    sample_rows = [
        _joined_row(
            uid,
            source=source[uid],
            recovery=recovery.get(uid),
            selection=selection[uid],
            discussiontools=discussiontools.get(uid),
            audit=audit,
            raw=raw,
            raw_states=raw_states,
            recovery_by_action=recovery_by_action,
            candidate_cache=candidate_cache,
        )
        for uid, audit in zip(sample_uids, audit_rows, strict=True)
    ]
    sample_table = table_from_union_pylist(sample_rows)
    atomic_parquet(bundle_paths.sample_evidence, sample_table)

    sample_reviews = [format_review_object(_review_projection(row)) for row in sample_rows]
    sample_review_bytes = _jsonl_bytes(sample_reviews)
    atomic_write_bytes(bundle_paths.sample_review, sample_review_bytes)

    unavailable_rows: list[dict[str, Any]] = []
    for uid in unavailable_uids:
        row = _joined_row(
            uid,
            source=source[uid],
            recovery=recovery.get(uid),
            selection=selection[uid],
            discussiontools=discussiontools.get(uid),
            audit=None,
            raw=raw,
            raw_states=raw_states,
            recovery_by_action=recovery_by_action,
            candidate_cache=candidate_cache,
        )
        target_state = raw_states.get(row.get("target_revision_id") or -1, {})
        predecessor_state = raw_states.get(row.get("predecessor_revision_id") or -1, {})
        row.update(
            {
                "target_raw_available": row.get("target_wikitext") is not None,
                "predecessor_raw_available": row.get("predecessor_wikitext") is not None,
                "target_cache_availability_status": target_state.get("availability_status"),
                "predecessor_cache_availability_status": predecessor_state.get(
                    "availability_status"
                ),
                "cache_resolution_failure": any(
                    state.get("availability_status") == "content_available"
                    and state.get("resolution") != "available"
                    for state in (target_state, predecessor_state)
                ),
            }
        )
        row["unavailable_taxonomy"] = unavailable_taxonomy(row)
        unavailable_rows.append(row)
    unavailable_table = table_from_union_pylist(unavailable_rows)
    atomic_parquet(bundle_paths.unavailable, unavailable_table)

    control_inputs: list[dict[str, Any]] = []
    for uid in precontrol_uids:
        joined = _joined_row(
            uid,
            source=source[uid],
            recovery=recovery.get(uid),
            selection=selection[uid],
            discussiontools=discussiontools.get(uid),
            audit=None,
            raw=raw,
            raw_states=raw_states,
            recovery_by_action=recovery_by_action,
            candidate_cache=candidate_cache,
        )
        control = _control_input(joined, source[uid], recovery.get(uid), selection[uid])
        if control is not None:
            control_inputs.append(control)
    controls = select_calibration_controls(
        control_inputs, sample_uids, size=CONTROL_COUNT, seed=seed
    )
    if len(controls.rows) != CONTROL_COUNT:
        raise RuntimeError(f"expected {CONTROL_COUNT} calibration controls")
    validate_calibration_key(controls.rows, controls.key)
    control_reviews = []
    secret = {
        "accepted_start",
        "accepted_end",
        "accepted_raw",
        "accepted_body",
        "accepted_provenance",
        "accepted_tier",
        "calibration_control_class",
    }
    for row in controls.rows:
        visible = {
            key: value for key, value in _review_projection(dict(row)).items() if key not in secret
        }
        visible["calibration_control"] = True
        control_reviews.append(format_review_object(visible))
    atomic_write_bytes(bundle_paths.calibration_controls, _jsonl_bytes(control_reviews))
    calibration_key_table = table_from_union_pylist(controls.key)
    atomic_parquet(bundle_paths.calibration_key, calibration_key_table)

    # Re-read the portable artifacts and validate their actual serialized form.
    portable_sample = pq.read_table(bundle_paths.sample_evidence).to_pylist()
    if len(portable_sample) != SAMPLE_COUNT:
        raise RuntimeError("portable sample row count mismatch")
    for expected, actual in zip(sample_design, portable_sample, strict=True):
        for field in DESIGN_FIELDS:
            if actual[field] != expected[field]:
                raise RuntimeError(f"frozen survey-design field changed: {field}")
    if canonical_json_hash([row["source_row_uid"] for row in portable_sample]) != sample_uid_hash:
        raise RuntimeError("portable sample UID/order hash mismatch")

    sample_window_checks = _validate_review_rows(sample_rows, sample_reviews)
    control_window_checks = _validate_review_rows(list(controls.rows), control_reviews)
    with bundle_paths.sample_review.open(encoding="utf-8") as handle:
        parsed_sample_review = [json.loads(line) for line in handle if line.strip()]
    with bundle_paths.calibration_controls.open(encoding="utf-8") as handle:
        parsed_controls = [json.loads(line) for line in handle if line.strip()]
    if len(parsed_sample_review) != SAMPLE_COUNT or len(parsed_controls) != CONTROL_COUNT:
        raise RuntimeError("JSONL row count/parseability check failed")
    if bundle_paths.sample_review.stat().st_size > 50 * 1024 * 1024:
        raise RuntimeError("sample_review.jsonl exceeds compactness limit")
    if bundle_paths.calibration_controls.stat().st_size > 5 * 1024 * 1024:
        raise RuntimeError("calibration_controls.jsonl exceeds compactness limit")

    frozen_after = _file_hashes(frozen_paths)
    if frozen_after != frozen_before:
        raise RuntimeError("frozen recovery/selection outputs changed during bundle generation")
    taxonomy_counts = Counter(row["unavailable_taxonomy"] for row in unavailable_rows)
    control_composition = {
        "tier": dict(
            sorted(Counter(row["calibration_control_class"] for row in controls.rows).items())
        ),
        "lifecycle": dict(
            sorted(Counter(row["calibration_lifecycle"] for row in controls.rows).items())
        ),
        "signature_state": dict(
            sorted(Counter(row["calibration_signature_state"] for row in controls.rows).items())
        ),
    }
    input_paths = [
        audit_paths.csv,
        audit_paths.manifest,
        method_paths.source_population,
        method_paths.recovery_evidence,
        method_paths.selection_audit,
        method_paths.revision_index,
    ]
    generic_index = settings.roots.output / "silver" / "talk_page_revision_observations.parquet"
    if generic_index.exists():
        input_paths.append(generic_index)
    if discussiontools_path.exists():
        input_paths.append(discussiontools_path)
    output_descriptors = {
        "sample_evidence.parquet": {
            **file_descriptor(bundle_paths.sample_evidence),
            "rows": sample_table.num_rows,
        },
        "sample_review.jsonl": {
            **file_descriptor(bundle_paths.sample_review),
            "rows": len(sample_reviews),
        },
        "unavailable_818.parquet": {
            **file_descriptor(bundle_paths.unavailable),
            "rows": unavailable_table.num_rows,
        },
        "calibration_controls.jsonl": {
            **file_descriptor(bundle_paths.calibration_controls),
            "rows": len(control_reviews),
        },
        "calibration_key.parquet": {
            **file_descriptor(bundle_paths.calibration_key),
            "rows": calibration_key_table.num_rows,
        },
    }
    candidate_count = sum(len(row["all_candidates"]) for row in sample_rows)
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "git_commit": _git_commit(repository_root),
        "generation_command": (
            "uv run wikidisputes-ssot revision-diff llm-audit-bundle "
            f"--config config/ssot.example.yaml --seed {seed}"
        ),
        "seed": seed,
        "inputs": {str(path): file_descriptor(path) for path in input_paths},
        "sample_uid_hash": sample_uid_hash,
        "expected_frozen_sample_uid_hash": audit_manifest["sample_source_uid_sha256"],
        "sample_design_hash": sample_design_hash,
        "row_counts": {
            "sample": SAMPLE_COUNT,
            "b_unavailable": UNAVAILABLE_COUNT,
            "calibration_controls": CONTROL_COUNT,
        },
        "joins": [
            "audit.csv left one-to-one source_row_uid -> method_b_source_population.parquet",
            "audit.csv left one-to-one source_row_uid -> method_b_recovery_evidence.parquet",
            "audit.csv left one-to-one source_row_uid -> method_b_selection_audit.parquet",
            "audit.csv optional source_row_uid -> discussiontools_feasibility_evidence.parquet",
            "target/predecessor revision IDs -> exact cache index and content-addressed response",
            "competing action_uid -> method_b_recovery_evidence.parquet",
        ],
        "schema": {
            "sample_evidence.parquet": _schema_text(sample_table),
            "unavailable_818.parquet": _schema_text(unavailable_table),
            "calibration_key.parquet": _schema_text(calibration_key_table),
            "sample_review.jsonl": sorted(sample_reviews[0]),
            "calibration_controls.jsonl": sorted(control_reviews[0]),
            "audit_csv_columns_preserved": audit_columns,
        },
        "missingness": _missingness(sample_rows, sample_reviews),
        "unavailable_taxonomy_counts": dict(sorted(taxonomy_counts.items())),
        "calibration_control_composition": control_composition,
        "quality_checks": {
            "exactly_600_unique_sample_uids": True,
            "frozen_sample_uid_hash_exact": True,
            "frozen_sample_order_exact": True,
            "survey_design_fields_byte_preserved": True,
            "exactly_818_unavailable_separate": True,
            "recovery_selection_outputs_unchanged": True,
            "candidate_intervals_slice_exactly": True,
            "structural_candidate_intervals_checked": candidate_count,
            "sample_review_windows": sample_window_checks,
            "control_review_windows": control_window_checks,
            "focal_ranges_never_internally_truncated": True,
            "controls_reproduce_accepted_boundaries": True,
            "jsonl_parseable": True,
            "jsonl_compactness_limits_bytes": {
                "sample_review": 50 * 1024 * 1024,
                "calibration_controls": 5 * 1024 * 1024,
            },
        },
        "frozen_output_sha256_before_after": {
            path: {"before": digest, "after": frozen_after[path]}
            for path, digest in frozen_before.items()
        },
        "outputs": output_descriptors,
    }
    atomic_write_json(bundle_paths.manifest, manifest)
    return {
        "bundle_directory": str(bundle_paths.root),
        "manifest": str(bundle_paths.manifest),
        "row_counts": manifest["row_counts"],
        "missingness": manifest["missingness"],
        "unavailable_taxonomy_counts": manifest["unavailable_taxonomy_counts"],
        "calibration_control_composition": control_composition,
        "quality_checks": manifest["quality_checks"],
        "outputs": output_descriptors,
    }
