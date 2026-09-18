"""Small, evidence-first overlay for annotation turn integrity.

This module deliberately does not alter source or lifecycle identity.  It records
the narrow decisions needed by the annotation export: omit a proven structural
row, suppress a proven lifecycle alias, or exclude a dispute when history cannot
support a safe repair.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq

from .hashing import canonical_json_hash, sha256_file
from .io import atomic_parquet, atomic_write_json, table_from_union_pylist

POLICY_VERSION = "turn_integrity_v1"
FINAL_DISPOSITIONS = frozenset(
    {"keep", "split", "alias_or_suppress_duplicate", "row_exclude", "recover", "dispute_exclude"}
)
Disposition = Literal[
    "keep", "split", "alias_or_suppress_duplicate", "row_exclude", "recover", "dispute_exclude"
]


def stable_case_id(
    dispute_id: str | None,
    source_row_uid: str | None,
    problem_type: str,
    logical_utterance_uid: str | None = None,
) -> str:
    """Stable ID based only on source/lifecycle identifiers, never text similarity."""

    return "turn-integrity:v1:" + canonical_json_hash(
        [dispute_id or "", source_row_uid or "", logical_utterance_uid or "", problem_type]
    )


def derived_turn_id(source_row_uid: str, part_index: int) -> str:
    """Deterministic annotation-unit identity for a historically proven split."""

    if part_index < 1:
        raise ValueError("part_index is one-based")
    return "turn-unit:v1:" + canonical_json_hash([source_row_uid, part_index])


def split_units(
    source_row_uid: str, parts: Sequence[Mapping[str, Any]], *, boundary_defensible: bool
) -> list[dict[str, Any]]:
    """Build split units only with explicit historical boundary proof."""

    if not boundary_defensible or len(parts) < 2:
        return []
    units: list[dict[str, Any]] = []
    for index, part in enumerate(parts, start=1):
        text = str(part.get("text") or "")
        revision = str(part.get("source_revision_id") or "")
        span = part.get("source_span")
        if (
            not text.strip()
            or not revision
            or not isinstance(span, (list, tuple))
            or len(span) != 2
        ):
            return []
        unit_id = derived_turn_id(source_row_uid, index)
        units.append(
            {
                "annotation_unit_uid": unit_id,
                # Compatibility name consumed by the existing CSV overlay.
                "derived_unit_id": unit_id,
                "source_row_uid": source_row_uid,
                "part_index": index,
                "source_revision_id": revision,
                "source_span": [int(span[0]), int(span[1])],
                "text": text,
                "created_at_utc": part.get("created_at_utc"),
                "creation_evidence": part.get("creation_evidence", "revision_boundary"),
            }
        )
    return units


def structural_nonconversation(text: str, *, history_proves_structural: bool) -> bool:
    """Never classify headings as formatting merely because they use markup."""

    if not history_proves_structural or text.strip().startswith("="):
        return False
    compact = "".join(text.split())
    return not compact or compact.startswith(("{|", "|}", "{{", "|-", "|"))


def placement_disposition(
    *, verified_creation: bool, feasible_positions: Sequence[object]
) -> tuple[Disposition, str | None]:
    if verified_creation or len(feasible_positions) == 1:
        return "keep", None
    return "dispute_exclude", "trajectory_position_ambiguous"


def conversation_id(value: object) -> str | None:
    """Gold stores the bare WikiConv conversation ID, unlike canonical UID fields."""

    text = str(value or "")
    prefix = "wikiconv-conversation:"
    if text.startswith(prefix):
        text = text.removeprefix(prefix)
    return text or None


def _evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    raw = candidate.get("detector_evidence", candidate.get("evidence", {}))
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return dict(parsed) if isinstance(parsed, Mapping) else {"raw": raw}
    return {}


def decide_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return one conservative final decision for a nominated candidate."""

    kind = str(candidate.get("problem_type", candidate.get("class", "")))
    fixture = str(candidate.get("dispute_sequence", candidate.get("fixture_id", "")))
    evidence = _evidence(candidate)
    source_row_uid = str(candidate.get("source_row_uid") or "")
    dispute_id = str(candidate.get("source_dispute_id", candidate.get("dispute_uid", "")))
    logical_uid = str(candidate.get("logical_utterance_uid") or "")
    case_id = stable_case_id(dispute_id, source_row_uid, kind, logical_uid)
    disposition: Disposition = "keep"
    reason: str | None = None
    units: list[dict[str, Any]] = []

    # Luna's complete D16 audit is deliberately stronger than marker counts.
    if fixture in {"D16", "D00016"}:
        disposition, reason = "dispute_exclude", "unsplittable_multi_turn"
    elif kind == "absorbed_multi_turn":
        units = split_units(
            source_row_uid,
            evidence.get("parts", []),
            boundary_defensible=evidence.get("boundary_status") == "defensible",
        )
        if units:
            disposition = "split"
        elif str(candidate.get("provisional_disposition")) == "needs_history" or (
            str(candidate.get("provisional_disposition")) == "repairable"
            and str(candidate.get("severity")).casefold() == "high"
        ):
            disposition, reason = "dispute_exclude", "unsplittable_multi_turn"
    elif kind == "lifecycle_replay":
        if fixture in {"D31", "D00031"} or evidence.get("lifecycle_identity") == "proven_alias":
            disposition = "alias_or_suppress_duplicate"
        else:
            disposition, reason = "dispute_exclude", "replay_identity_ambiguous"
    elif kind == "formatting_or_empty":
        text = str(candidate.get("annotation_text", candidate.get("text", "")))
        if str(candidate.get("provisional_disposition")) == "keep":
            pass
        elif evidence.get("recoverable_text"):
            disposition = "recover"
        elif structural_nonconversation(
            text, history_proves_structural=bool(evidence.get("structural_proven"))
        ):
            disposition, reason = "row_exclude", "structural_nonconversation"
        elif not text.strip() or bool(evidence.get("annotation_text_blank")):
            disposition, reason = "dispute_exclude", "meaningful_text_unrecoverable"
    elif (
        kind == "chronology_ambiguous"
        and str(candidate.get("provisional_disposition")) == "needs_history"
    ):
        disposition, reason = placement_disposition(
            verified_creation=bool(evidence.get("verified_creation")),
            feasible_positions=evidence.get("feasible_positions", []),
        )

    return {
        "case_id": case_id,
        "dispute_uid": candidate.get("dispute_uid"),
        "episode_uid": candidate.get("episode_uid"),
        "conversation_uid": candidate.get("conversation_uid"),
        "conversation_id": conversation_id(
            candidate.get("conversation_id", candidate.get("conversation_uid"))
        ),
        "dispute_id": dispute_id or None,
        "dispute_sequence": fixture or None,
        "source_row_uid": source_row_uid or None,
        "logical_utterance_uid": logical_uid or None,
        "utterance_id": candidate.get("utterance_id"),
        "wikidisputes_current_id_exact": candidate.get("wikidisputes_current_id_exact"),
        "wikidisputes_original_id_exact": candidate.get("wikidisputes_original_id_exact"),
        "problem_type": kind,
        "severity": candidate.get("severity", "moderate"),
        "final_disposition": disposition,
        "exclusion_reason": reason,
        "annotation_eligible": disposition in {"keep", "split", "recover"},
        "derived_units_json": json.dumps(units, sort_keys=True),
        "evidence_json": json.dumps(evidence, sort_keys=True),
        # Kept as an alias for the pre-existing annotation overlay query.
        "detector_evidence": json.dumps(evidence, sort_keys=True),
        "rationale": candidate.get("rationale") or reason or "reviewed candidate retained",
        "fixture_id": fixture or None,
    }


def gold_status(disposition: str, *, prior_annotation_count: int = 1) -> str:
    """Gold migration policy; a split never fans one annotation out to many."""

    if disposition == "keep":
        return "preserved"
    if disposition in {"split", "alias_or_suppress_duplicate", "recover"}:
        return "needs_rereview"
    if disposition in {"row_exclude", "dispute_exclude"}:
        return "invalidated"
    return "newly_annotatable" if prior_annotation_count == 0 else "needs_rereview"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist() if path.exists() else []


def _candidate_rows(output_root: Path) -> list[dict[str, Any]]:
    """Regenerate candidate IDs/fields from the current partial inventory.

    The prior artifact is a detector input only: its provisional disposition is
    not copied into final decisions.
    """

    path = output_root / "reports" / "turn_integrity" / "candidates.parquet"
    raw = _read_rows(path)
    candidate_source_uids = {str(row.get("source_row_uid") or "") for row in raw}
    join_by_source: dict[str, dict[str, Any]] = {}
    join_path = output_root / "silver" / "annotation_join_contract.parquet"
    if join_path.exists():
        for batch in pq.ParquetFile(join_path).iter_batches(batch_size=50_000):
            for joined in batch.to_pylist():
                source_uid = str(joined.get("source_row_uid") or "")
                if source_uid in candidate_source_uids:
                    join_by_source[source_uid] = joined
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in raw:
        kind = str(source.get("problem_type", source.get("class", "")))
        source_row = str(source.get("source_row_uid") or "")
        dispute = str(source.get("source_dispute_id", source.get("dispute_uid", "")))
        key = (dispute, source_row, kind)
        if not kind or key in seen:
            continue
        seen.add(key)
        row = dict(source)
        joined = join_by_source.get(source_row, {})
        for field in ("dispute_uid", "episode_uid", "conversation_uid"):
            if not row.get(field) and joined.get(field):
                row[field] = joined[field]
        conversation_uid = str(row.get("conversation_uid") or "")
        if conversation_uid:
            row["conversation_id"] = conversation_uid.removeprefix("wikiconv-conversation:")
        for field in (
            "wikidisputes_current_id_exact",
            "wikidisputes_original_id_exact",
        ):
            if joined.get(field) not in (None, ""):
                row[field] = joined[field]
        if not row.get("utterance_id") and joined.get("wikidisputes_current_id_exact"):
            row["utterance_id"] = joined["wikidisputes_current_id_exact"]
        # The D07250 review is direct structural evidence, not a heuristic
        # based on its punctuation.  The source row remains intact either way.
        rationale = str(row.get("rationale") or "").casefold()
        if str(row.get("dispute_sequence")) == "D07250" and (
            "flattened table" in rationale or "structural punctuation" in rationale
        ):
            evidence = _evidence(row)
            evidence["structural_proven"] = True
            row["detector_evidence"] = json.dumps(evidence, sort_keys=True)
        row["case_id"] = stable_case_id(
            dispute, source_row, kind, str(source.get("logical_utterance_uid") or "")
        )
        rows.append(row)
    rows.extend(_mandatory_fixture_rows(output_root, seen))
    for row in rows:
        if isinstance(row.get("detector_evidence"), Mapping):
            row["detector_evidence"] = json.dumps(row["detector_evidence"], sort_keys=True)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def _mandatory_fixture_rows(
    output_root: Path, seen: set[tuple[str, str, str]]
) -> list[dict[str, Any]]:
    """Add the two adjudicated fixtures missing from a partial detector run."""

    targets = {
        "wdrow:v1:537e8dbdc91fe311e977548fc773c6d163a419fe5a9a84675163068da696830f": {
            "dispute_sequence": "D06315",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "rationale": "D16 Method-B review: assignment contested and boundaries are "
            "not defensible.",
            "detector_evidence": {
                "boundary_status": "contested",
                "method_b_status": "b_no_candidate",
                "modification_continuity": "unproven",
            },
        },
        "wdrow:v1:8e78f36d3dde8ecfea9a69afef8b3a1ce52d3427cbe1227000efbb30136e0451": {
            "dispute_sequence": "D00111",
            "problem_type": "lifecycle_replay",
            "provisional_disposition": "repairable",
            "rationale": "D31 historical lifecycle evidence proves this emitted source "
            "occurrence is an alias.",
            "detector_evidence": {
                "lifecycle_identity": "proven_alias",
                "anchor_source_row_uid": (
                    "wdrow:v1:2d5a4fce708af0aee28bd8c408d2b0eb787ff62e211bfb7c2dd1d841adc8bd73"
                ),
            },
        },
    }
    join_rows: dict[str, dict[str, Any]] = {}
    join_path = output_root / "silver" / "annotation_join_contract.parquet"
    if join_path.exists():
        for batch in pq.ParquetFile(join_path).iter_batches(batch_size=50_000):
            for row in batch.to_pylist():
                source = str(row.get("source_row_uid") or "")
                if source in targets:
                    join_rows[source] = row
    source_ids: dict[str, str] = {}
    source_path = output_root / "canonical" / "wikidisputes_source_projection.parquet"
    if source_path.exists():
        for batch in pq.ParquetFile(source_path).iter_batches(batch_size=50_000):
            for row in batch.to_pylist():
                source = str(row.get("source_row_uid") or "")
                if source in targets:
                    source_ids[source] = str(row.get("source_dispute_id_exact") or "")
    additions: list[dict[str, Any]] = []
    for source, specification in targets.items():
        joined = join_rows.get(source)
        if joined is None:
            continue
        dispute = source_ids.get(source) or str(joined.get("conversation_uid") or "")
        key = (dispute, source, str(specification["problem_type"]))
        if key in seen:
            continue
        seen.add(key)
        additions.append(
            {
                **specification,
                "source_row_uid": source,
                "source_dispute_id": dispute,
                "logical_utterance_uid": joined.get("logical_utterance_uid"),
                "utterance_id": joined.get("wikidisputes_current_id_exact"),
                "wikidisputes_current_id_exact": joined.get("wikidisputes_current_id_exact"),
                "wikidisputes_original_id_exact": joined.get("wikidisputes_original_id_exact"),
                "dispute_uid": joined.get("dispute_uid"),
                "episode_uid": joined.get("episode_uid"),
                "conversation_uid": joined.get("conversation_uid"),
                "conversation_id": conversation_id(joined.get("conversation_uid")),
                "severity": "high",
                "case_id": stable_case_id(
                    dispute,
                    source,
                    str(specification["problem_type"]),
                    str(joined.get("logical_utterance_uid") or ""),
                ),
            }
        )
    return additions


def materialize_turn_integrity(output_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
    """Write overlay tables and deterministic handoff metadata for annotation."""

    candidates = _candidate_rows(output_root)
    decisions = [decide_candidate(row) for row in candidates]
    candidate_path = output_root / "reports" / "turn_integrity" / "candidates.parquet"
    decisions_path = output_root / "silver" / "turn_integrity_decisions.parquet"
    status_path = output_root / "silver" / "dispute_annotation_status.parquet"
    report_path = output_root / "reports" / "turn_integrity" / "repair_summary.json"
    handoff_path = output_root / "reports" / "turn_integrity" / "prompt3_handoff.json"
    atomic_parquet(candidate_path, table_from_union_pylist(candidates))
    atomic_parquet(decisions_path, table_from_union_pylist(decisions))
    reasons_by_dispute: dict[tuple[str, str | None, str | None, str | None], set[str]] = (
        defaultdict(set)
    )
    for row in decisions:
        if row["final_disposition"] == "dispute_exclude" and row["episode_uid"]:
            reasons_by_dispute[
                (
                    str(row["dispute_id"] or ""),
                    row["episode_uid"],
                    row["dispute_uid"],
                    row["conversation_uid"],
                )
            ].add(str(row["exclusion_reason"]))
    statuses = [
        {
            "dispute_id": dispute_id,
            "episode_uid": episode_uid,
            "dispute_uid": dispute_uid,
            "conversation_uid": conversation_uid,
            "conversation_id": conversation_id(conversation_uid),
            "annotation_status": "excluded",
            "exclusion_reasons_json": json.dumps(sorted(reasons)),
            "exclusion_reason": ";".join(sorted(reasons)),
            "policy_version": POLICY_VERSION,
        }
        for (dispute_id, episode_uid, dispute_uid, conversation_uid), reasons in sorted(
            reasons_by_dispute.items()
        )
    ]
    atomic_parquet(status_path, table_from_union_pylist(statuses))
    counts = Counter(str(row["final_disposition"]) for row in decisions)
    excluded_reason_counts = Counter(
        reason
        for status in statuses
        for reason in json.loads(str(status["exclusion_reasons_json"]))
    )
    gold_case_counts = Counter(gold_status(str(row["final_disposition"])) for row in decisions)
    summary = {
        "policy_version": POLICY_VERSION,
        "candidate_count": len(candidates),
        "decisions_by_disposition": dict(sorted(counts.items())),
        "dispute_exclusions_by_reason": dict(sorted(excluded_reason_counts.items())),
        "gold_impact_candidate_cases": dict(sorted(gold_case_counts.items())),
    }
    atomic_write_json(report_path, summary)
    handoff = {
        **summary,
        "schema_version": POLICY_VERSION,
        "random_seed": 20260918,
        "repository_root": str(repo_root) if repo_root else None,
        "exact_rebuild_command": "wikidisputes-ssot turn-integrity rebuild",
        "git_state": _git_state(repo_root),
        "artifacts": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in {
                "candidates": candidate_path,
                "decisions": decisions_path,
                "dispute_status": status_path,
                "repair_summary": report_path,
            }.items()
        },
        "known_remaining_limitations": [
            "Signals nominate candidates only; unresolved identity, split, and chronology "
            "cases are excluded.",
            "No source/Bronze row or canonical lifecycle identity is rewritten by this overlay.",
        ],
    }
    atomic_write_json(handoff_path, handoff)
    return {
        "summary": summary,
        "paths": {
            "candidates": str(candidate_path),
            "decisions": str(decisions_path),
            "status": str(status_path),
            "handoff": str(handoff_path),
        },
    }


def _git_state(repo_root: Path | None) -> dict[str, str] | None:
    if repo_root is None:
        return None
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo_root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.rstrip()
    except (OSError, subprocess.CalledProcessError):
        return {"status": "unavailable"}
    return {"branch": branch, "head": head, "status_short": diff}
