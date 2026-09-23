from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import duckdb
from openpyxl import load_workbook

from .io import atomic_write_bytes, atomic_write_json

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output"
CANONICAL = OUTPUT / "canonical"
SILVER = OUTPUT / "silver"
ANNOTATION = OUTPUT / "annotation"
REPORTS = OUTPUT / "reports"
FINAL_SELECTION = SILVER / "method_b_combined_representation.parquet"
METHOD_B_BASELINE = ANNOTATION / "wikidisputes_llm_annotation_input.method_b_baseline.csv"
METHOD_B_STAGED = ANNOTATION / "wikidisputes_llm_annotation_input.method_b_staged.csv"
TURN_INTEGRITY_DECISIONS = SILVER / "turn_integrity_decisions.parquet"
DISPUTE_ANNOTATION_STATUS = SILVER / "dispute_annotation_status.parquet"
VALIDATION_DECISION = ROOT / "config" / "decisions" / "method_b_validation_decision.json"
ANNOTATION_EXCLUSIONS = ROOT / "config" / "decisions" / "annotation_exclusions.json"
FINAL_GOLD_NAME = "gold_input_ssot_annotation_ready.xlsx"
EXPECTED_GOLD_COLUMNS = 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _accepted_decision() -> dict[str, Any]:
    decision = json.loads(VALIDATION_DECISION.read_text(encoding="utf-8"))
    if decision.get("method_b_accepted") is not True:
        raise RuntimeError("Method-B selection has not been accepted")
    return decision


def _annotation_exclusions() -> list[dict[str, str]]:
    payload = json.loads(ANNOTATION_EXCLUSIONS.read_text(encoding="utf-8"))
    exclusions = payload.get("exclusions")
    if not isinstance(exclusions, list):
        raise RuntimeError("annotation exclusions must contain an exclusions list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for exclusion in exclusions:
        dispute_id = str(exclusion.get("dispute_id") or "")
        label = str(exclusion.get("dispute_label") or "")
        reason = str(exclusion.get("reason") or "")
        if not dispute_id or not label or not reason or dispute_id in seen:
            raise RuntimeError(f"invalid annotation exclusion: {exclusion!r}")
        seen.add(dispute_id)
        result.append({"dispute_id": dispute_id, "dispute_label": label, "reason": reason})
    return result


def qpath(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def _escalated_label(value: object) -> int:
    """Convert the source dispute outcome to the Gold sheet's binary label."""
    if value is True or str(value).strip().casefold() in {"true", "1"}:
        return 1
    if value is False or str(value).strip().casefold() in {"false", "0"}:
        return 0
    raise RuntimeError(f"annotation row has no binary dispute escalation: {value!r}")


def _validate_annotation_escalation(csv_path: Path) -> None:
    """Require every final unit to agree with its dispute's source outcome."""
    labels_by_dispute: dict[str, int] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not {"dispute_id", "escalated"} <= set(reader.fieldnames):
            raise RuntimeError("annotation export lacks dispute escalation columns")
        for row in reader:
            dispute_id = str(row["dispute_id"] or "")
            if not dispute_id:
                raise RuntimeError("annotation row has no dispute identity")
            label = _escalated_label(row["escalated"])
            previous = labels_by_dispute.setdefault(dispute_id, label)
            if label != previous:
                raise RuntimeError(f"conflicting escalation labels in dispute {dispute_id}")


def _turn_integrity_overlay(csv_path: Path) -> dict[str, Any]:
    """Apply the additive annotation-unit eligibility overlay.

    The canonical source-occurrence export is deliberately materialized first.
    This routine only removes units with a documented final decision and expands
    a source row when a historical boundary decision supplies derived units.
    It therefore never changes Bronze/SSOT identities or writes a repaired
    canonical table.
    """

    if not TURN_INTEGRITY_DECISIONS.exists():
        return {"applied": False, "source_rows_suppressed": 0, "derived_rows": 0}
    if not DISPUTE_ANNOTATION_STATUS.exists():
        raise RuntimeError("turn-integrity decisions exist without dispute annotation status")

    connection = duckdb.connect()
    try:
        decision_columns = {
            str(row[0])
            for row in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{qpath(TURN_INTEGRITY_DECISIONS)}')"
            ).fetchall()
        }
        required = {"source_row_uid", "final_disposition", "derived_units_json"}
        if missing := sorted(required - decision_columns):
            raise RuntimeError(f"turn-integrity decisions missing required columns: {missing}")
        optional_decision_columns = {
            name: f"COALESCE({name}, '')" if name in decision_columns else "''"
            for name in (
                "decision_reason",
                "annotation_representation",
                "annotation_text_source",
                "fallback_text",
                "fallback_text_source",
            )
        }
        decision_rows = connection.execute(
            "SELECT source_row_uid, final_disposition, derived_units_json, "
            "COALESCE(exclusion_reason, ''), COALESCE(case_id, ''), "
            "COALESCE(detector_evidence, ''), "
            f"{optional_decision_columns['decision_reason']}, "
            f"{optional_decision_columns['annotation_representation']}, "
            f"{optional_decision_columns['annotation_text_source']}, "
            f"{optional_decision_columns['fallback_text']}, "
            f"{optional_decision_columns['fallback_text_source']} "
            f"FROM read_parquet('{qpath(TURN_INTEGRITY_DECISIONS)}')"
        ).fetchall()
        status_columns = {
            str(row[0])
            for row in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{qpath(DISPUTE_ANNOTATION_STATUS)}')"
            ).fetchall()
        }
        if "annotation_status" in status_columns:
            episode_column = "episode_uid" if "episode_uid" in status_columns else "dispute_uid"
            status_rows = connection.execute(
                f"SELECT {episode_column}, annotation_status, COALESCE(exclusion_reason, '') "
                f"FROM read_parquet('{qpath(DISPUTE_ANNOTATION_STATUS)}')"
            ).fetchall()
        else:
            # An empty status artifact has Arrow's placeholder schema.  Row
            # exclusions remain decision-local, so it means no episode-wide
            # suppression rather than a malformed status table.
            status_rows = []
    finally:
        connection.close()

    excluded_episodes = {
        str(episode): str(reason)
        for episode, status, reason in status_rows
        if str(status).casefold() in {"exclude", "excluded", "dispute_exclude"}
    }
    decisions_by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
    for (
        source_uid,
        disposition,
        derived_json,
        reason,
        case_id,
        evidence,
        decision_reason,
        representation,
        annotation_text_source,
        fallback_text,
        fallback_text_source,
    ) in decision_rows:
        decisions_by_source[str(source_uid)].append(
            {
                "final_disposition": str(disposition),
                "derived_units_json": "" if derived_json is None else str(derived_json),
                "reason": str(reason),
                "case_id": str(case_id),
                "evidence": str(evidence),
                "decision_reason": str(decision_reason),
                "annotation_representation": str(representation),
                "annotation_text_source": str(annotation_text_source),
                "fallback_text": str(fallback_text),
                "fallback_text_source": str(fallback_text_source),
            }
        )
    reattachments_by_target: dict[str, dict[str, str]] = {}
    for source_uid, candidates in decisions_by_source.items():
        for candidate in candidates:
            if candidate["final_disposition"] != "alias_or_suppress_duplicate":
                continue
            try:
                evidence = json.loads(candidate["evidence"])
            except json.JSONDecodeError:
                continue
            target = str(evidence.get("reattach_target_source_uid") or "")
            if not target:
                continue
            if target in reattachments_by_target:
                raise RuntimeError(f"multiple fragment reattachments target {target}")
            reattachments_by_target[target] = {
                "fragment_source_uid": source_uid,
                "case_id": candidate["case_id"],
                "evidence": candidate["evidence"],
                "append_text": str(evidence.get("append_text") or ""),
                "joiner": str(evidence.get("joiner") or " "),
            }

    # Keep only explicit lifecycle anchors from turn-integrity evidence.  A
    # reply may name a suppressed historical representation, so the resolver
    # receives this source-level map after overlay decisions are known.  Text
    # and similarity are intentionally absent from this mapping.
    source_anchor_map = _source_anchor_map(decisions_by_source)

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise RuntimeError("annotation export has no header")
        fieldnames = list(reader.fieldnames)
        for field in (
            "ssot_annotation_unit_uid",
            "ssot_turn_integrity_disposition",
            "ssot_turn_integrity_case_id",
            "ssot_turn_integrity_evidence",
            "ssot_turn_integrity_decision_reason",
            "ssot_annotation_representation",
            "ssot_turn_integrity_part_index",
            "needs_rereview",
            "reply_to_utterance_id",
            "reply_to_utterance_id_raw",
            "reply_to_utterance_order",
            "ssot_reply_resolution_status",
        ):
            if field not in fieldnames:
                fieldnames.append(field)
        source_rows = list(reader)
        source_rows_by_uid = {str(row.get("ssot_source_row_uid") or ""): row for row in source_rows}
        output: list[dict[str, str]] = []
        suppressed = derived = blank = 0
        for row in source_rows:
            episode = str(row.get("ssot_episode_uid") or "")
            source_uid = str(row.get("ssot_source_row_uid") or "")
            candidates = decisions_by_source.get(source_uid, [])
            reattachment = reattachments_by_target.get(source_uid)
            decision = next(
                (item for item in candidates if item["final_disposition"] == "split"),
                next(
                    (
                        item
                        for item in candidates
                        if item["final_disposition"]
                        in {"row_exclude", "alias_or_suppress_duplicate"}
                    ),
                    next(
                        (
                            item
                            for item in candidates
                            if item["final_disposition"] == "wikidisputes_fallback"
                        ),
                        next(
                            (
                                item
                                for item in candidates
                                if item["final_disposition"] == "recover" and item["fallback_text"]
                            ),
                            None,
                        ),
                    ),
                ),
            )
            if episode in excluded_episodes:
                suppressed += 1
                continue
            if decision and decision["final_disposition"] == "split":
                try:
                    units = json.loads(decision["derived_units_json"])
                except json.JSONDecodeError as error:
                    raise RuntimeError(
                        f"invalid derived turn JSON for {source_uid}: {error}"
                    ) from error
                if not isinstance(units, list) or not units:
                    raise RuntimeError(f"split decision for {source_uid} has no derived turns")
                for unit in units:
                    if not isinstance(unit, dict):
                        raise RuntimeError(f"split decision for {source_uid} has invalid turn")
                    text = str(unit.get("text") or "")
                    if not text.strip():
                        raise RuntimeError(
                            f"split decision for {source_uid} emits blank annotation unit"
                        )
                    replacement = dict(row)
                    replacement["utterance_id"] = str(unit.get("derived_unit_id") or "")
                    replacement["utterance_text"] = text
                    replacement["speaker_id"] = str(
                        unit.get("speaker_id") or replacement.get("speaker_id") or ""
                    )
                    timestamp = unit.get("created_at_utc")
                    if timestamp:
                        replacement["timestamp"] = str(timestamp)
                        replacement["timestamp_semantics"] = "creation_utc"
                    replacement["ssot_annotation_unit_uid"] = replacement["utterance_id"]
                    replacement["ssot_turn_integrity_disposition"] = "split"
                    replacement["ssot_turn_integrity_case_id"] = decision["case_id"]
                    replacement["ssot_turn_integrity_evidence"] = decision["evidence"]
                    replacement["ssot_turn_integrity_part_index"] = str(unit.get("part_index") or 0)
                    replacement["needs_rereview"] = "true"
                    output.append(replacement)
                    derived += 1
                suppressed += 1
                continue
            if decision:
                if decision["final_disposition"] in {"wikidisputes_fallback", "recover"}:
                    # A decision supplies exact Method-A text from the
                    # immutable WikiDisputes source record.  Never retain an
                    # unsafe Method-B/reconstructed variant for this row.
                    row["utterance_text"] = decision["fallback_text"]
                    if not row["utterance_text"].strip():
                        raise RuntimeError(
                            f"wikidisputes fallback has no authoritative text: {source_uid}"
                        )
                    row["ssot_annotation_text_source"] = (
                        decision["fallback_text_source"] or decision["annotation_text_source"]
                    )
                    row["ssot_annotation_unit_uid"] = source_uid
                    row["ssot_turn_integrity_disposition"] = decision["final_disposition"]
                    row["ssot_turn_integrity_case_id"] = decision["case_id"]
                    row["ssot_turn_integrity_evidence"] = decision["evidence"]
                    row["ssot_turn_integrity_decision_reason"] = decision["decision_reason"]
                    row["ssot_annotation_representation"] = decision["annotation_representation"]
                    row["ssot_turn_integrity_part_index"] = "0"
                    row["needs_rereview"] = "true"
                    if not str(row["utterance_text"] or "").strip():
                        blank += 1
                    output.append(row)
                    continue
                suppressed += 1
                continue
            if not str(row.get("utterance_text") or "").strip():
                # An included blank is a failed integrity decision, not a
                # recoverable export condition.
                blank += 1
                raise RuntimeError(f"included blank annotation unit: {source_uid}")
            row["ssot_annotation_unit_uid"] = source_uid
            row["ssot_turn_integrity_disposition"] = "keep"
            row["ssot_turn_integrity_case_id"] = ""
            row["ssot_turn_integrity_evidence"] = ""
            row["ssot_turn_integrity_decision_reason"] = ""
            row["ssot_annotation_representation"] = ""
            row["ssot_turn_integrity_part_index"] = "0"
            # A population detector case may correctly remain included.  Keep
            # its case/evidence on the export so a `keep` cannot silently
            # bypass turn-integrity review merely because it needs no repair.
            kept_case = next(
                (item for item in candidates if item["final_disposition"] == "keep"), None
            )
            unresolved_keep = False
            if kept_case:
                row["ssot_turn_integrity_case_id"] = kept_case["case_id"]
                row["ssot_turn_integrity_evidence"] = kept_case["evidence"]
                row["ssot_turn_integrity_decision_reason"] = kept_case["decision_reason"]
                unresolved_keep = str(kept_case["decision_reason"]).startswith("unresolved_")
            # Actor repairs must be backed by an exact signature in a named
            # source occurrence.  They are recorded as ordinary kept
            # decisions, so apply only the explicit per-source evidence here.
            for candidate in candidates:
                try:
                    evidence = json.loads(candidate["evidence"])
                except json.JSONDecodeError:
                    continue
                replacement = evidence.get("speaker_replacement")
                if (
                    candidate["final_disposition"] == "keep"
                    and evidence.get("actor_signature_status")
                    in {"proven_alias", "proven_speaker_replacement"}
                    and isinstance(replacement, str)
                    and replacement
                ):
                    if row.get("speaker_id") != replacement:
                        unresolved_keep = True
                    row["speaker_id"] = replacement
                    row["ssot_turn_integrity_case_id"] = candidate["case_id"]
                    row["ssot_turn_integrity_evidence"] = candidate["evidence"]
                    row["ssot_turn_integrity_decision_reason"] = (
                        candidate["decision_reason"] or "speaker_repaired_from_explicit_signature"
                    )
                    break
            if reattachment:
                fragment_text = reattachment["append_text"] or str(row.get("utterance_text") or "")
                # The dynamic branch uses the fragment row's already-selected
                # annotation text.  It is still a direct source-neighbor
                # reattachment, not a text-search reconstruction.
                if not reattachment["append_text"]:
                    if len(decisions_by_source[reattachment["fragment_source_uid"]]) != 1:
                        raise RuntimeError("fragment reattachment lacks one source decision")
                    fragment_row = source_rows_by_uid.get(reattachment["fragment_source_uid"])
                    if fragment_row is None:
                        raise RuntimeError("fragment reattachment source row is absent")
                    fragment_text = str(fragment_row.get("utterance_text") or "")
                if not fragment_text.strip():
                    raise RuntimeError("fragment reattachment has blank source text")
                row["utterance_text"] = (
                    f"{str(row.get('utterance_text') or '').rstrip()}"
                    f"{reattachment['joiner']}{fragment_text.lstrip()}"
                )
                row["ssot_annotation_text_source"] = "turn_integrity_immediate_neighbor_reattach"
                row["ssot_turn_integrity_disposition"] = "reattached_fragment"
                row["ssot_turn_integrity_case_id"] = reattachment["case_id"]
                row["ssot_turn_integrity_evidence"] = reattachment["evidence"]
                row["ssot_turn_integrity_decision_reason"] = "fragment_reattached"
                row["needs_rereview"] = "true"
            else:
                row["needs_rereview"] = str(unresolved_keep).lower()
            output.append(row)

    output.sort(
        key=lambda row: (
            row.get("dispute_sequence", ""),
            int(row.get("substantive_order") or 2**63),
            int(row.get("ssot_turn_integrity_part_index") or 0),
            row.get("ssot_annotation_unit_uid", ""),
        )
    )
    source_alias_map: defaultdict[str, set[str]] = defaultdict(set)
    for source_uid, source_row in source_rows_by_uid.items():
        for field in ("utterance_id", "original_utterance_id"):
            alias = str(source_row.get(field) or "")
            if alias:
                source_alias_map[alias].add(source_uid)
    reply_report = _resolve_overlay_replies(
        output,
        source_anchor_map=source_anchor_map,
        source_alias_map=source_alias_map,
    )
    unit_ids = [str(row.get("ssot_annotation_unit_uid") or "") for row in output]
    if not all(unit_ids) or len(unit_ids) != len(set(unit_ids)):
        raise RuntimeError("annotation integrity overlay emitted duplicate or missing unit IDs")
    if any(not str(row.get("utterance_text") or "").strip() for row in output):
        raise RuntimeError("annotation integrity overlay emitted blank annotation text")
    for row in output:
        row["ssot_text_differs_from_source"] = str(
            row["utterance_text"] != row["ssot_source_text_exact"]
        ).lower()
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(output)
    atomic_write_bytes(csv_path, buffer.getvalue().encode("utf-8"))
    return {
        "applied": True,
        "source_rows_suppressed": suppressed,
        "derived_rows": derived,
        "included_rows": len(output),
        "included_source_rows": len({str(row.get("ssot_source_row_uid") or "") for row in output}),
        "unique_annotation_units": len(unit_ids),
        "excluded_disputes": len(excluded_episodes),
        "included_blank_rows": blank,
        "wikidisputes_fallback_rows": sum(
            row.get("ssot_turn_integrity_disposition") == "wikidisputes_fallback" for row in output
        ),
        "wikidisputes_fallback_blank_rows": sum(
            row.get("ssot_turn_integrity_disposition") == "wikidisputes_fallback"
            and not str(row.get("utterance_text") or "").strip()
            for row in output
        ),
        "reply_resolution": reply_report,
    }


def _resolve_overlay_replies(
    rows: list[dict[str, str]],
    *,
    source_anchor_map: Mapping[str, set[str]] | None = None,
    source_alias_map: Mapping[str, set[str]] | None = None,
) -> dict[str, int]:
    """Resolve annotation-facing replies after source rows are retained/split.

    ``reply_to_utterance_id_raw`` is copied from the source projection and is
    never rewritten.  A resolved reply is allowed only when exactly one
    earlier retained unit in the same episode matches the raw target by an
    explicit ID alias.  In particular, a source target replaced by multiple
    split children remains unresolved instead of selecting a child by text or
    order.
    """

    source_anchor_map = source_anchor_map or {}
    source_alias_map = source_alias_map or {}

    def aliases(row: dict[str, str]) -> set[str]:
        return {
            str(row.get(field) or "")
            for field in ("utterance_id", "original_utterance_id")
            if str(row.get(field) or "")
        }

    def terminal_sources(source_uid: str) -> set[str]:
        """Follow explicit anchor links, retaining ambiguity/cycles as empty."""

        current = {source_uid}
        seen: set[str] = set()
        while True:
            next_sources: set[str] = set()
            for value in current:
                if value in seen:
                    return set()
                seen.add(value)
                anchors = set(source_anchor_map.get(value, set()))
                next_sources.update(anchors or {value})
            if next_sources == current:
                return current
            current = next_sources

    by_episode: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_episode[str(row.get("ssot_episode_uid") or row.get("dispute_sequence") or "")].append(
            row
        )
    for episode_rows in by_episode.values():
        episode_rows.sort(
            key=lambda row: (
                int(row.get("substantive_order") or 2**63),
                int(row.get("ssot_turn_integrity_part_index") or 0),
                str(row.get("ssot_annotation_unit_uid") or ""),
            )
        )

    resolved = unresolved = ambiguous = 0
    for row in rows:
        raw = str(row.get("reply_to_utterance_id_raw") or row.get("reply_to_utterance_id") or "")
        # Keep this source-level provenance even for rows which have no
        # retained target after the overlay.
        row["reply_to_utterance_id_raw"] = raw
        row["reply_to_utterance_id"] = ""
        row["reply_to_utterance_order"] = ""
        if not raw:
            row["ssot_reply_resolution_status"] = "unresolved_no_raw_target"
            unresolved += 1
            continue

        episode = str(row.get("ssot_episode_uid") or row.get("dispute_sequence") or "")
        episode_rows = by_episode[episode]
        try:
            row_position = episode_rows.index(row)
        except ValueError:
            row["ssot_reply_resolution_status"] = "unresolved_row_not_retained"
            unresolved += 1
            continue
        all_candidates = [candidate for candidate in episode_rows if raw in aliases(candidate)]
        if not all_candidates and source_alias_map:
            terminal_uids: set[str] = set()
            for source_uid in source_alias_map.get(raw, set()):
                terminal_uids.update(terminal_sources(source_uid))
            all_candidates = [
                candidate
                for candidate in episode_rows
                if str(candidate.get("ssot_source_row_uid") or "") in terminal_uids
            ]
        if len(all_candidates) > 1:
            row["ssot_reply_resolution_status"] = "unresolved_ambiguous_split_target"
            ambiguous += 1
            unresolved += 1
            continue
        candidates = [
            candidate for candidate in all_candidates if candidate in episode_rows[:row_position]
        ]
        if len(candidates) == 1:
            target = candidates[0]
            row["reply_to_utterance_id"] = str(target.get("utterance_id") or "")
            row["reply_to_utterance_order"] = str(
                target.get("utterance_order") or target.get("substantive_order") or ""
            )
            row["ssot_reply_resolution_status"] = "resolved_after_turn_integrity"
            resolved += 1
        elif len(candidates) > 1:
            row["ssot_reply_resolution_status"] = "unresolved_ambiguous_split_target"
            ambiguous += 1
            unresolved += 1
        else:
            row["ssot_reply_resolution_status"] = "unresolved_no_retained_target"
            unresolved += 1

    # Defensive invariant: a resolved ID must be the ID of an earlier row in
    # the same episode.  This is intentionally checked after all rewrites.
    for _episode, episode_rows in by_episode.items():
        positions = {
            str(row.get("utterance_id") or ""): index for index, row in enumerate(episode_rows)
        }
        for index, row in enumerate(episode_rows):
            target = str(row.get("reply_to_utterance_id") or "")
            if target and (positions.get(target) is None or positions[target] >= index):
                raise RuntimeError(
                    "resolved reply does not target an earlier retained row in the same dispute"
                )
    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "ambiguous_split_targets": ambiguous,
        "invariant_passed": 1,
    }


def _source_anchor_map(
    decisions_by_source: Mapping[str, list[dict[str, str]]],
) -> defaultdict[str, set[str]]:
    """Build reply aliases only from final, lifecycle-proven suppressions."""

    anchors: defaultdict[str, set[str]] = defaultdict(set)
    for source_uid, candidates in decisions_by_source.items():
        for candidate in candidates:
            if candidate.get("final_disposition") != "alias_or_suppress_duplicate":
                continue
            try:
                evidence = json.loads(candidate.get("evidence") or "{}")
            except json.JSONDecodeError:
                continue
            if not _proven_lifecycle_anchor(evidence):
                continue
            anchor = str(evidence.get("anchor_source_row_uid") or "")
            if anchor:
                anchors[str(source_uid)].add(anchor)
    return anchors


def _proven_lifecycle_anchor(evidence: Mapping[str, Any]) -> bool:
    """Require the same explicit physical-comment proof used for aliasing."""

    if evidence.get("lifecycle_identity") == "proven_alias":
        return True
    slot = evidence.get("physical_comment_slot")
    if not isinstance(slot, Mapping):
        slot = evidence
    return bool(
        (slot.get("stable_across_revisions") or slot.get("stable_physical_comment_slot"))
        and (slot.get("action_coordinate") or slot.get("wikiconv_action_coordinate"))
        and (
            slot.get("root_evidence")
            or slot.get("structural_coordinate")
            or slot.get("wikiconv_root_coordinate")
        )
        and (slot.get("anchor_source_row_uid") or evidence.get("anchor_source_row_uid"))
    )


def setup(con: duckdb.DuckDBPyConnection) -> None:
    files = {
        "j": SILVER / "annotation_join_contract.parquet",
        "sp": CANONICAL / "wikidisputes_source_projection.parquet",
        "u": CANONICAL / "wikidisputes_utterances_ssot.parquet",
        "r": SILVER / "utterance_representations.parquet",
        "a": SILVER / "utterance_actions.parquet",
        "disp": CANONICAL / "wikidisputes_annotation_display.parquet",
        "re": SILVER / "reply_edges.parquet",
        "mwr": SILVER / "mediawiki_raw_comment_representations.parquet",
    }

    missing = [str(p) for p in files.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Required completed SSOT outputs are missing:\n" + "\n".join(missing)
        )

    for name, path in files.items():
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{qpath(path)}')")


def all_source_sql() -> str:
    return """
    SELECT
        j.join_row_uid,
        j.source_row_uid,
        j.dispute_uid,
        j.episode_uid,
        j.conversation_uid,
        j.logical_utterance_uid,
        j.context_node_uid,
        j.annotation_eligible AS annotation_eligible,
        j.utterance_order AS join_utterance_order,
        j.display_order AS join_display_order,

        j.wikidisputes_current_id_exact,
        j.wikidisputes_original_id_exact,
        j.wikidisputes_text_exact AS source_text_exact,
        COALESCE(
            u.wikiconv_speaker_exact,
            u.wikidisputes_user_exact,
            j.wikidisputes_user_exact
        ) AS source_user_exact,

        sp.source_case_index,
        sp.source_row_index,
        sp.source_order,
        sp.source_dispute_id_exact,
        sp.source_wikidisputes_escalated,
        sp.wikidisputes_conv_id_exact,
        sp.wikidisputes_reply_to_exact,
        sp.wikidisputes_time,
        sp.wikidisputes_type_exact,
        sp.wikidisputes_pagetitle_exact,

        MAX(sp.wikidisputes_pagetitle_exact)
            OVER (PARTITION BY j.episode_uid) AS episode_page_title,

        u.created_at_utc AS ssot_created_at_utc,
        u.created_at_status AS ssot_created_at_status,
        u.chronology_eligible AS ssot_chronology_eligible,
        u.chronology_status AS ssot_chronology_status,
        u.chronology_rank AS ssot_chronology_rank,
        u.display_utterance_order AS canonical_display_utterance_order,
        u.display_order AS canonical_display_order,
        u.was_modified AS ssot_was_modified,
        u.recovery_status AS ssot_recovery_status,
        u.final_text_representation_uid,
        sourceact.action_type AS source_action_type,
        sourceact.raw_timestamp AS action_timestamp_raw,

        re.raw_reply_target AS ssot_raw_reply_target,
        re.target_logical_utterance_uid AS ssot_reply_target_logical_uid,
        re.target_utterance_order AS ssot_reply_target_utterance_order,
        re.resolution_status AS ssot_reply_resolution_status,

        CASE
            WHEN j.context_node_uid IS NOT NULL THEN
                COALESCE(
                    NULLIF(dc.text_exact, ''),
                    CASE WHEN NULLIF(TRIM(j.wikidisputes_text_exact), '') IS NOT NULL THEN j.wikidisputes_text_exact END
                )
            ELSE
                COALESCE(

                    CASE WHEN NULLIF(TRIM(mwbody.content_inline), '') IS NOT NULL THEN mwbody.content_inline END,
                    CASE WHEN NULLIF(TRIM(finalr.content_inline), '') IS NOT NULL THEN finalr.content_inline END,
                    CASE WHEN NULLIF(TRIM(fallbackr.content_inline), '') IS NOT NULL THEN fallbackr.content_inline END,
                    CASE WHEN NULLIF(TRIM(du.text_exact), '') IS NOT NULL THEN du.text_exact END,
                    CASE WHEN NULLIF(TRIM(j.wikidisputes_text_exact), '') IS NOT NULL THEN j.wikidisputes_text_exact END
                )
        END AS annotation_text,

        CASE
            WHEN j.context_node_uid IS NOT NULL
                THEN 'context_exact'
            WHEN CASE WHEN NULLIF(TRIM(mwbody.content_inline), '') IS NOT NULL THEN mwbody.content_inline END IS NOT NULL
                THEN 'mediawiki_revision_comment_wikitext_body'
            WHEN CASE WHEN NULLIF(TRIM(finalr.content_inline), '') IS NOT NULL THEN finalr.content_inline END IS NOT NULL
                THEN 'wikiconv_final_text_exact'
            WHEN CASE WHEN NULLIF(TRIM(fallbackr.content_inline), '') IS NOT NULL THEN fallbackr.content_inline END IS NOT NULL
                THEN fallbackr.representation_kind
            WHEN CASE WHEN NULLIF(TRIM(du.text_exact), '') IS NOT NULL THEN du.text_exact END IS NOT NULL
                THEN 'annotation_display_exact'
            ELSE 'wikidisputes_text_exact'
        END AS annotation_text_source

    FROM j
    JOIN sp
      ON sp.source_row_uid = j.source_row_uid

    LEFT JOIN u
      ON u.logical_utterance_uid = j.logical_utterance_uid

    LEFT JOIN LATERAL (
        SELECT
            act.version_uid,
            act.action_uid,
            act.action_type,
            act.raw_timestamp
        FROM a act
        WHERE act.logical_utterance_uid = j.logical_utterance_uid
          AND CAST(act.action_id_exact AS VARCHAR)
              = CAST(sp.wikidisputes_id_exact AS VARCHAR)
        ORDER BY
            CASE
                WHEN act.source_row_uid = j.source_row_uid THEN 0
                ELSE 1
            END,
            act.action_uid
        LIMIT 1
    ) sourceact ON TRUE

    LEFT JOIN LATERAL (
        SELECT
            rr.content_inline,
            rr.representation_uid,
            rr.confidence
        FROM r rr
        WHERE rr.logical_utterance_uid = j.logical_utterance_uid
          AND rr.version_uid = sourceact.version_uid
          AND rr.representation_kind = 'utterance_wikitext_fragment'
          AND rr.availability_status = 'recovered'
          AND NULLIF(rr.content_inline, '') IS NOT NULL
        ORDER BY rr.representation_uid
        LIMIT 1
    ) sourcefrag ON TRUE



    /* Historical raw MediaWiki body that passed the independent
       promotion-safety gate for this exact source occurrence.
       V3.3 high-confidence alone is not sufficient. */
    LEFT JOIN LATERAL (
        SELECT
            rr.content_inline,
            rr.representation_uid,
            rr.confidence,
            rr.source_revision_id,
            rr.best_similarity,
            rr.match_margin
        FROM mwr rr
        WHERE rr.logical_utterance_uid =
              j.logical_utterance_uid
          AND rr.source_row_uid =
              j.source_row_uid
          AND rr.representation_kind =
              'mediawiki_revision_comment_wikitext_body'
          AND rr.availability_status =
              'recovered'
          AND rr.confidence =
              'high_confidence_comment_match'
          AND rr.promotion_safety_decision =
              'promote'
          AND NULLIF(
                  TRIM(rr.content_inline),
                  ''
              ) IS NOT NULL
        ORDER BY rr.representation_uid
        LIMIT 1
    ) mwbody ON TRUE
    LEFT JOIN r finalr
      ON finalr.representation_uid = u.final_text_representation_uid

    LEFT JOIN LATERAL (
        SELECT
            rr.content_inline,
            rr.representation_kind
        FROM r rr
        WHERE rr.logical_utterance_uid = j.logical_utterance_uid
          AND NULLIF(rr.content_inline, '') IS NOT NULL
          AND rr.representation_kind IN (
              'wikiconv_action_text_exact',
              'wikidisputes_text_exact'
          )
        ORDER BY
            CASE
                WHEN rr.representation_kind =
                     'wikiconv_action_text_exact' THEN 0
                ELSE 1
            END,
            rr.available_at DESC NULLS LAST,
            rr.representation_uid
        LIMIT 1
    ) fallbackr ON TRUE

    LEFT JOIN re
      ON re.source_logical_utterance_uid = j.logical_utterance_uid

    LEFT JOIN disp du
      ON du.logical_utterance_uid = j.logical_utterance_uid
     AND du.row_kind = 'utterance'

    LEFT JOIN disp dc
      ON dc.context_node_uid = j.context_node_uid
     AND dc.row_kind = 'context'
    """


def _substantive_order_clause() -> str:
    """Return the total order used for annotation-facing substantive order.

    ``join_display_order`` is the canonical integrated order produced by the
    chronology pipeline: known creation times, deterministic equal-time ties,
    and constrained reply/action placement for unresolved rows.  Reusing it
    here preserves feasible-interval placement instead of moving every
    unresolved row after every known row.  The stable source keys make the
    result total without inferring a timestamp or an identity.
    """

    return """
        join_display_order NULLS LAST,
        source_order,
        source_row_uid
    """


def entity_sql() -> str:
    return f"""
    WITH raw AS (
        {all_source_sql()}
    ),
    ranked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY
                    episode_uid,
                    COALESCE(logical_utterance_uid, context_node_uid)
                ORDER BY
                    CASE
                        WHEN context_node_uid IS NOT NULL THEN 0
                        WHEN wikidisputes_type_exact = 'original' THEN 0
                        ELSE 1
                    END,
                    source_order,
                    source_row_uid
            ) AS entity_rank
        FROM raw
        WHERE logical_utterance_uid IS NOT NULL
           OR context_node_uid IS NOT NULL
    )
    SELECT * EXCLUDE(entity_rank)
    FROM ranked
    WHERE entity_rank = 1
    """


def full_export_sql() -> str:
    return f"""
    WITH raw AS (
        {all_source_sql()}
    ),
    numbered AS (
        SELECT
            *,
            DENSE_RANK() OVER (
                ORDER BY episode_uid
            ) AS dispute_number
        FROM raw
    ),
    ordered AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY episode_uid
                ORDER BY
                    join_display_order NULLS LAST,
                    source_order,
                    source_row_uid
            ) AS local_display_order,

            ROW_NUMBER() OVER (
                PARTITION BY episode_uid
                ORDER BY
                    {_substantive_order_clause()}
            ) AS local_substantive_order
        FROM numbered
    )
    SELECT
        'D' || LPAD(CAST(o.dispute_number AS VARCHAR), 5, '0')
            AS dispute_sequence,

        COALESCE(
            o.source_dispute_id_exact,
            o.wikidisputes_conv_id_exact,
            o.episode_uid
        ) AS dispute_id,

        o.episode_page_title AS dispute_label,

        o.source_wikidisputes_escalated AS escalated,

        o.ssot_chronology_rank AS utterance_order,

        o.local_substantive_order AS substantive_order,

        -- Context remains canonical provenance, but it is not an
        -- annotation-facing exclusion or a special first-row role.
        'utterance' AS utterance_role,

        o.wikidisputes_current_id_exact AS utterance_id,
        o.wikidisputes_original_id_exact AS original_utterance_id,
        o.source_user_exact AS speaker_id,

        CAST(o.ssot_created_at_utc AS VARCHAR) AS timestamp,

        o.wikidisputes_reply_to_exact AS reply_to_utterance_id,
        o.wikidisputes_reply_to_exact AS reply_to_utterance_id_raw,
        target.chronology_rank AS reply_to_utterance_order,

        o.wikidisputes_type_exact AS utterance_type,
        o.episode_page_title AS source_page_title,
        o.annotation_text AS utterance_text,

        CASE
            WHEN o.wikidisputes_current_id_exact IS NULL THEN NULL
            ELSE
                'https://en.wikipedia.org/w/index.php?oldid=' ||
                split_part(o.wikidisputes_current_id_exact, '.', 1)
        END AS wikipedia_revision_url,

        o.source_row_uid AS ssot_source_row_uid,
        o.annotation_eligible,
        o.logical_utterance_uid AS ssot_logical_utterance_uid,
        o.context_node_uid AS ssot_context_node_uid,
        CASE
            WHEN o.context_node_uid IS NOT NULL THEN 'context'
            ELSE 'utterance'
        END AS ssot_row_provenance,
        o.episode_uid AS ssot_episode_uid,
        o.conversation_uid AS ssot_conversation_uid,

        o.ssot_chronology_eligible,
        o.ssot_chronology_status,
        o.ssot_chronology_rank,
        o.local_display_order AS display_order,
        o.canonical_display_utterance_order AS ssot_display_utterance_order,
        o.canonical_display_order AS ssot_canonical_display_order,
        o.ssot_created_at_status,
        o.wikidisputes_time AS ssot_raw_source_timestamp,
        o.action_timestamp_raw AS ssot_action_timestamp_raw,
        o.source_action_type AS ssot_action_type,
        CASE
            WHEN o.ssot_created_at_utc IS NOT NULL THEN 'creation_utc'
            ELSE 'creation_time_unresolved'
        END AS timestamp_semantics,
        o.ssot_reply_target_logical_uid,
        o.ssot_reply_target_utterance_order,
        o.ssot_reply_resolution_status,

        o.annotation_text_source AS ssot_annotation_text_source,
        o.source_text_exact AS ssot_source_text_exact,

        CASE
            WHEN o.annotation_text IS DISTINCT FROM o.source_text_exact
                THEN TRUE
            ELSE FALSE
        END AS ssot_text_differs_from_source,

        o.ssot_was_modified,
        o.ssot_recovery_status

    FROM ordered o

    LEFT JOIN (
        SELECT DISTINCT
            episode_uid,
            logical_utterance_uid,
            ssot_chronology_rank AS chronology_rank
        FROM raw
        WHERE logical_utterance_uid IS NOT NULL
    ) target
      ON target.episode_uid = o.episode_uid
     AND target.logical_utterance_uid = o.ssot_reply_target_logical_uid

    ORDER BY
        o.dispute_number,
        o.join_display_order NULLS LAST,
        o.source_order,
        o.source_row_uid
    """


def dict_rows(
    con: duckdb.DuckDBPyConnection,
    query: str,
) -> list[dict[str, Any]]:
    cur = con.execute(query)
    names = [x[0] for x in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _apply_method_b_text_selection(row: dict[str, str], selection: tuple[str, Any] | None) -> bool:
    """Apply selected text and refresh its comparison with the exact source."""
    method_b = bool(selection and selection[0] == "method_b")
    if method_b:
        row["utterance_text"] = "" if selection[1] is None else str(selection[1])
        row["ssot_annotation_text_source"] = "mediawiki_revision_diff_comment_wikitext_body"
    row["ssot_text_differs_from_source"] = str(
        row["utterance_text"] != row["ssot_source_text_exact"]
    ).lower()
    return method_b


def export_full(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    ANNOTATION.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    csv_path = ANNOTATION / "wikidisputes_llm_annotation_input.csv"
    research_key = ANNOTATION / "wikidisputes_annotation_research_key.csv"
    _accepted_decision()

    query = full_export_sql()

    con.execute(f"COPY ({query}) TO '{qpath(csv_path)}' (FORMAT CSV, HEADER, DELIMITER ',')")

    # Preserve the exact pre-Method-B product.  Stage-7 invariants compare this
    # file with ``METHOD_B_STAGED`` so later eligibility overlays cannot be
    # mistaken for a Method-B structural mutation.
    atomic_write_bytes(METHOD_B_BASELINE, csv_path.read_bytes())

    selected = {
        str(row[0]): (str(row[1]), row[2])
        for row in con.execute(
            "SELECT source_row_uid, selected_method, selected_text "
            f"FROM read_parquet('{qpath(FINAL_SELECTION)}')"
        ).fetchall()
    }
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise RuntimeError("annotation export has no header")
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=reader.fieldnames, lineterminator="\n")
        writer.writeheader()
        rows = 0
        method_b_rows = 0
        for row in reader:
            rows += 1
            selection = selected.get(row.get("ssot_source_row_uid", ""))
            if _apply_method_b_text_selection(row, selection):
                method_b_rows += 1
            writer.writerow(row)
    final_bytes = buffer.getvalue().encode("utf-8")
    atomic_write_bytes(METHOD_B_STAGED, final_bytes)
    atomic_write_json(
        METHOD_B_STAGED.with_suffix(".json"),
        {
            "status": "materialized_pre_turn_integrity",
            "baseline": {
                "path": str(METHOD_B_BASELINE),
                "sha256": _sha256(METHOD_B_BASELINE),
            },
            "output": {
                "path": str(METHOD_B_STAGED),
                "sha256": _sha256(METHOD_B_STAGED),
            },
            "rows": rows,
            "method_b_rows": method_b_rows,
            "turn_integrity_overlay_applied": False,
        },
    )
    atomic_write_bytes(csv_path, final_bytes)
    integrity_report = _turn_integrity_overlay(csv_path)
    _validate_annotation_escalation(csv_path)

    con.execute(
        f"""
        COPY (
            SELECT DISTINCT
                episode_uid AS ssot_episode_uid,
                dispute_uid AS ssot_dispute_uid,
                source_wikidisputes_escalated AS escalated
            FROM ({entity_sql()})
            ORDER BY ssot_episode_uid
        )
        TO '{qpath(research_key)}'
        (FORMAT CSV, HEADER, DELIMITER ',')
        """
    )

    counts = dict_rows(
        con,
        f"""
        WITH x AS ({query})
        SELECT
            COUNT(*) AS total_rows,
            COUNT(DISTINCT ssot_source_row_uid) AS distinct_source_rows,
            COUNT(*) FILTER (WHERE annotation_eligible) AS annotation_eligible_rows,
            COUNT(*) FILTER (
                WHERE utterance_role = 'utterance'
            ) AS utterance_rows,
            COUNT(*) FILTER (
                WHERE utterance_role = 'context'
            ) AS context_rows,
            COUNT(*) FILTER (
                WHERE ssot_context_node_uid IS NOT NULL
            ) AS context_classified_rows,
            COUNT(DISTINCT ssot_episode_uid) AS disputes,
            COUNT(DISTINCT ssot_logical_utterance_uid) FILTER (
                WHERE utterance_role = 'utterance'
            ) AS distinct_logical_utterances,
            COUNT(*) FILTER (
                WHERE utterance_role = 'utterance'
                  AND (
                      utterance_text IS NULL
                      OR LENGTH(TRIM(utterance_text)) = 0
                  )
            ) AS empty_utterance_text
        FROM x
        """,
    )[0]

    duplicate_count = con.execute(
        f"""
        WITH x AS ({query})
        SELECT COUNT(*)
        FROM (
            SELECT ssot_source_row_uid, COUNT(*) AS n
            FROM x
            GROUP BY 1
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]

    if duplicate_count:
        raise RuntimeError(
            f"Full export contains {duplicate_count} duplicate source-row identities."
        )
    # ``counts`` describes the immutable source-occurrence projection, before
    # the annotation integrity overlay.  The final CSV is validated below by
    # the overlay itself: it contains no blank unit and has only documented
    # source suppressions or evidence-backed split children.

    readiness = _annotation_readiness()
    report = {
        "status": readiness["status"],
        "annotation_ready": readiness["annotation_ready"],
        "annotation_readiness": readiness,
        "annotation_csv": str(csv_path),
        "research_key_csv": str(research_key),
        **counts,
        "source_rows": counts["distinct_source_rows"],
        "annotation_eligible_rows": counts["annotation_eligible_rows"],
        "duplicate_source_rows": duplicate_count,
        "outcome_columns_in_annotation_csv": [],
        "method_b_rows": method_b_rows,
        "selection_artifact": str(FINAL_SELECTION),
        "method_b_baseline": {
            "path": str(METHOD_B_BASELINE),
            "sha256": _sha256(METHOD_B_BASELINE),
        },
        "method_b_staged": {
            "path": str(METHOD_B_STAGED),
            "sha256": _sha256(METHOD_B_STAGED),
        },
        "turn_integrity": integrity_report,
    }

    (REPORTS / "annotation_export_report.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    return report


def _gold_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Match every source row as a codable annotation utterance.

    Older Gold shells can contain a descriptive ``context`` role.  It is not
    an annotation identity and is deliberately normalized here so the row is
    matched to the current, codable source-occurrence export.
    """

    return (
        str(row.get("dispute_id") or ""),
        str(row.get("utterance_id") or ""),
        "utterance",
    )


def _workbook_values(path: Path) -> list[tuple[str, list[list[Any]]]]:
    workbook = load_workbook(path, data_only=False)
    return [
        (sheet.title, [[cell.value for cell in row] for row in sheet.iter_rows()])
        for sheet in workbook.worksheets
    ]


def _natural_key(value: Any) -> tuple[tuple[int, int | str], ...]:
    """Return a deterministic ascending key for identifiers such as D01 or D12."""

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(value or ""))
        if part
    )


def _numeric_order(value: Any, *, row_number: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"Gold row {row_number} has non-numeric utterance_order {value!r}")
    if isinstance(value, str):
        # New rows originate in the CSV export, whose numeric fields are
        # strings.  Preserve the same positivity/integrality contract as an
        # existing numeric Gold cell without rejecting an otherwise valid
        # restored fallback row during display sorting.
        text = value.strip()
        if not text.isdigit():
            raise RuntimeError(f"Gold row {row_number} has non-numeric utterance_order {value!r}")
        numeric = int(text)
        if numeric < 1 or text != str(numeric):
            raise RuntimeError(f"Gold row {row_number} has invalid utterance_order {value!r}")
        return numeric
    try:
        numeric = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f"Gold row {row_number} has non-numeric utterance_order {value!r}"
        ) from error
    if numeric < 1 or numeric != value:
        raise RuntimeError(f"Gold row {row_number} has invalid utterance_order {value!r}")
    return numeric


def _gold_turn_integrity_state() -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    """Return excluded dispute/conversation IDs and changed source occurrences.

    The Gold shell uses its historical conversation ``dispute_id`` whereas the
    source export also carries episode IDs.  The status table deliberately
    retains both, so this lookup is a migration aid rather than a new identity
    resolver.
    """

    if not TURN_INTEGRITY_DECISIONS.exists() or not DISPUTE_ANNOTATION_STATUS.exists():
        return set(), set(), set(), set(), set()
    con = duckdb.connect()
    try:
        split_sources = {
            str(row[0])
            for row in con.execute(
                "SELECT source_row_uid FROM read_parquet(?) "
                "WHERE final_disposition IN ('split', 'alias_or_suppress_duplicate', 'row_exclude')",
                [str(TURN_INTEGRITY_DECISIONS)],
            ).fetchall()
        }
        changed_sources = {
            str(row[0])
            for row in con.execute(
                "SELECT source_row_uid FROM read_parquet(?) "
                "WHERE final_disposition IN ('split', 'alias_or_suppress_duplicate')",
                [str(TURN_INTEGRITY_DECISIONS)],
            ).fetchall()
        }
        changed_aliases: set[str] = set()
        rereview_conversations = {
            str(row[0])
            for row in con.execute(
                "SELECT conversation_id FROM read_parquet(?) "
                "WHERE final_disposition IN ('split', 'alias_or_suppress_duplicate') "
                "AND conversation_id IS NOT NULL",
                [str(TURN_INTEGRITY_DECISIONS)],
            ).fetchall()
        }
        projection = CANONICAL / "wikidisputes_source_projection.parquet"
        # Gold rows carry WikiDisputes IDs rather than source-row UIDs.  Map
        # every suppressed/split source occurrence, not just aliases/splits,
        # so an invalidated cumulative representation cannot remain as an
        # unmatched stale Gold annotation shell.
        if split_sources and projection.exists():
            placeholders = ", ".join("?" for _ in split_sources)
            changed_aliases = {
                str(value)
                for row in con.execute(
                    "SELECT wikidisputes_id_exact, wikidisputes_original_id_exact "
                    f"FROM read_parquet(?) WHERE source_row_uid IN ({placeholders})",
                    [str(projection), *sorted(split_sources)],
                ).fetchall()
                for value in row
                if value not in (None, "")
            }
        fields = {
            str(row[0])
            for row in con.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(DISPUTE_ANNOTATION_STATUS)]
            ).fetchall()
        }
        identifiers = [
            field for field in ("conversation_id", "dispute_id", "episode_uid") if field in fields
        ]
        if not identifiers:
            return (
                set(),
                split_sources,
                changed_sources,
                changed_aliases,
                rereview_conversations,
            )
        excluded: set[str] = set()
        for identifier in identifiers:
            excluded.update(
                str(row[0])
                for row in con.execute(
                    f"SELECT {identifier} FROM read_parquet(?) "
                    "WHERE annotation_status IN ('exclude', 'excluded', 'dispute_exclude') "
                    f"AND {identifier} IS NOT NULL",
                    [str(DISPUTE_ANNOTATION_STATUS)],
                ).fetchall()
            )
        return (
            excluded,
            split_sources,
            changed_sources,
            changed_aliases,
            rereview_conversations,
        )
    finally:
        con.close()


def _sort_gold_rows(
    sheet: Any,
    headers: list[str],
    display_orders: dict[int, int] | None = None,
    turn_integrity_part_indexes: dict[int, int] | None = None,
) -> None:
    """Sort for display while preserving nullable chronology ranks in Gold."""

    header_index = {name: index + 1 for index, name in enumerate(headers)}
    split_groups: defaultdict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    for row_number in range(2, sheet.max_row + 1):
        part_index = (turn_integrity_part_indexes or {}).get(row_number, 0)
        if part_index <= 0:
            continue
        substantive_order = _numeric_order(
            sheet.cell(row_number, header_index["substantive_order"]).value,
            row_number=row_number,
        )
        if substantive_order is None:
            continue
        display_order = (display_orders or {}).get(row_number)
        if display_order is None:
            display_order = _numeric_order(
                sheet.cell(row_number, header_index["utterance_order"]).value,
                row_number=row_number,
            )
        split_groups[
            (str(sheet.cell(row_number, header_index["dispute_sequence"]).value), substantive_order)
        ].append((row_number, display_order if display_order is not None else 2**63))
    split_group_start = {
        row_number: min(display for _, display in members)
        for members in split_groups.values()
        if len(members) > 1
        for row_number, _ in members
    }
    records: list[tuple[tuple[Any, ...], list[dict[str, Any]]]] = []
    for row_number in range(2, sheet.max_row + 1):
        sequence = sheet.cell(row_number, header_index["dispute_sequence"]).value
        role = str(sheet.cell(row_number, header_index["utterance_role"]).value or "")
        if role not in {"context", "utterance"}:
            raise RuntimeError(f"Gold row {row_number} has invalid utterance_role {role!r}")
        chronology_rank = _numeric_order(
            sheet.cell(row_number, header_index["utterance_order"]).value,
            row_number=row_number,
        )
        display_order = (display_orders or {}).get(row_number)
        if display_order is None:
            display_order = chronology_rank
        if display_order is None:
            display_order = 2**63
        # Split units inherit a source row's display position.  Their
        # historically proven source-part order must resolve that exact tie
        # before display/UID fallbacks (e.g. Still, Belchfire, Still in D01057).
        part_index = (turn_integrity_part_indexes or {}).get(row_number, 0)

        cells = []
        for cell in sheet[row_number]:
            cells.append(
                {
                    "value": cell.value,
                    "style": copy.copy(cell._style),
                    "hyperlink": copy.copy(cell.hyperlink),
                    "comment": copy.copy(cell.comment),
                }
            )
        records.append(
            (
                (
                    _natural_key(sequence),
                    split_group_start.get(row_number, display_order),
                    part_index,
                    display_order,
                    chronology_rank if chronology_rank is not None else 2**63,
                    str(sheet.cell(row_number, header_index["utterance_id"]).value or ""),
                ),
                cells,
            )
        )

    records.sort(key=lambda record: record[0])
    previous_sequence: str | None = None
    previous_rank: int | None = None
    for row_number, (_, cells) in enumerate(records, start=2):
        for column_number, state in enumerate(cells, start=1):
            cell = sheet.cell(row_number, column_number)
            cell.value = state["value"]
            cell._style = copy.copy(state["style"])
            cell.hyperlink = copy.copy(state["hyperlink"])
            cell.comment = copy.copy(state["comment"])

        sequence = str(sheet.cell(row_number, header_index["dispute_sequence"]).value)
        if sequence != previous_sequence:
            previous_sequence = sequence
            previous_rank = None
        chronology_rank = _numeric_order(
            sheet.cell(row_number, header_index["utterance_order"]).value,
            row_number=row_number,
        )
        if chronology_rank is not None:
            if previous_rank is not None and chronology_rank < previous_rank:
                raise RuntimeError(f"Gold dispute {sequence!r} has creation ranks out of order")
            previous_rank = chronology_rank


def _assert_gold_disputes_complete(
    sheet: Any,
    headers: list[str],
    annotation_rows: list[dict[str, str]],
) -> int:
    """Require every represented Gold dispute to equal its final SSOT population."""

    header_index = {name: index + 1 for index, name in enumerate(headers)}
    gold_units: defaultdict[str, Counter[str]] = defaultdict(Counter)
    gold_sequences: defaultdict[str, set[str]] = defaultdict(set)
    gold_labels: defaultdict[str, set[str]] = defaultdict(set)
    for row_number in range(2, sheet.max_row + 1):
        dispute_id = str(sheet.cell(row_number, header_index["dispute_id"]).value or "")
        unit_id = str(sheet.cell(row_number, header_index["utterance_id"]).value or "")
        gold_units[dispute_id][unit_id] += 1
        gold_sequences[dispute_id].add(
            str(sheet.cell(row_number, header_index["dispute_sequence"]).value or "")
        )
        gold_labels[dispute_id].add(
            str(sheet.cell(row_number, header_index["dispute_label"]).value or "")
        )

    ssot_units: defaultdict[str, Counter[str]] = defaultdict(Counter)
    ssot_sequences: defaultdict[str, set[str]] = defaultdict(set)
    ssot_labels: defaultdict[str, set[str]] = defaultdict(set)
    for row in annotation_rows:
        dispute_id = str(row.get("dispute_id") or "")
        ssot_units[dispute_id][str(row.get("utterance_id") or "")] += 1
        sequence = str(row.get("dispute_sequence") or "")
        label = str(row.get("dispute_label") or "")
        if sequence:
            ssot_sequences[dispute_id].add(sequence)
        if label:
            ssot_labels[dispute_id].add(label)

    for dispute_id, units in gold_units.items():
        if not dispute_id or units != ssot_units.get(dispute_id, Counter()):
            missing = ssot_units.get(dispute_id, Counter()) - units
            extra = units - ssot_units.get(dispute_id, Counter())
            raise RuntimeError(
                f"Gold dispute {dispute_id!r} is not one complete current SSOT dispute: "
                f"missing={dict(missing)}, extra={dict(extra)}"
            )
        current_sequences = ssot_sequences.get(dispute_id, set())
        current_labels = ssot_labels.get(dispute_id, set())
        if (current_sequences and gold_sequences[dispute_id] != current_sequences) or (
            current_labels and gold_labels[dispute_id] != current_labels
        ):
            raise RuntimeError(
                f"Gold dispute {dispute_id!r} has stale sequence/label identity: "
                f"gold_sequences={sorted(gold_sequences[dispute_id])}, "
                f"current_sequences={sorted(current_sequences)}, "
                f"gold_labels={sorted(gold_labels[dispute_id])}, "
                f"current_labels={sorted(current_labels)}"
            )
    return len(gold_units)


def export_annotation_ready_gold(gold_path: Path, annotation_csv: Path) -> dict[str, Any]:
    """Build the 20-column annotation shell plus one explicit provenance column."""

    workbook = load_workbook(gold_path)
    if "Gold_Annotation" not in workbook.sheetnames:
        raise RuntimeError("Gold workbook does not contain Gold_Annotation")
    sheet = workbook["Gold_Annotation"]
    source_headers = [str(cell.value) for cell in sheet[1]]
    has_prior_provenance = source_headers[-1:] == ["provenance"]
    if has_prior_provenance:
        source_headers = source_headers[:-1]
    if len(source_headers) < EXPECTED_GOLD_COLUMNS:
        raise RuntimeError(
            "Gold input must contain at least the 20-column annotation shell; "
            f"found {sheet.max_column} columns"
        )
    headers = source_headers[:EXPECTED_GOLD_COLUMNS]
    if sheet.max_column > EXPECTED_GOLD_COLUMNS:
        sheet.delete_cols(
            EXPECTED_GOLD_COLUMNS + 1,
            sheet.max_column - EXPECTED_GOLD_COLUMNS,
        )
    forbidden = [name for name in headers if name.startswith("ssot_") or name.endswith("_legacy")]
    if forbidden:
        raise RuntimeError(f"engineering columns are forbidden in annotation Gold: {forbidden}")
    required = {
        "dispute_sequence",
        "dispute_id",
        "utterance_order",
        "utterance_id",
        "utterance_role",
        "utterance_text",
    }
    if missing := sorted(required - set(headers)):
        raise RuntimeError(f"Gold input is missing required columns: {missing}")

    # A prior fallback implementation appended the source population to this
    # human Gold sample. Population additions have no original shell
    # ``escalated`` value; retain only the sample rows and derived split units
    # that replace a sampled source occurrence.
    # This is deliberately a one-way repair of that known output shape, not a
    # sampling rule or a way to add fallback rows to Gold.
    if has_prior_provenance and "escalated" in headers:
        escalated_column = headers.index("escalated") + 1
        utterance_id_column = headers.index("utterance_id") + 1
        for row_number in range(sheet.max_row, 1, -1):
            unit_id = str(sheet.cell(row_number, utterance_id_column).value or "")
            if sheet.cell(row_number, escalated_column).value in (
                None,
                "",
            ) and not unit_id.startswith("turn-unit:v1:"):
                sheet.delete_rows(row_number)

    exclusions = _annotation_exclusions()
    excluded_ids = {row["dispute_id"] for row in exclusions}
    (
        integrity_excluded_ids,
        integrity_changed_sources,
        _integrity_rereview_sources,
        integrity_changed_aliases,
        _integrity_rereview_conversations,
    ) = _gold_turn_integrity_state()
    excluded_ids.update(integrity_excluded_ids)
    dispute_id_col = headers.index("dispute_id") + 1
    invalidated_rows = 0
    for row_number in range(sheet.max_row, 1, -1):
        if str(sheet.cell(row_number, dispute_id_col).value or "") in excluded_ids:
            sheet.delete_rows(row_number)
            invalidated_rows += 1

    with annotation_csv.open("r", encoding="utf-8", newline="") as handle:
        annotation_rows = list(csv.DictReader(handle))
    blocking_decisions: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    if TURN_INTEGRITY_DECISIONS.exists():
        connection = duckdb.connect()
        try:
            for source_uid, case_id, reason in connection.execute(
                "SELECT source_row_uid, case_id, annotation_blocking_reason "
                "FROM read_parquet(?) WHERE annotation_blocking = TRUE",
                [str(TURN_INTEGRITY_DECISIONS)],
            ).fetchall():
                blocking_decisions[str(source_uid)].append(
                    {
                        "case_id": str(case_id or ""),
                        "type": str(reason or "unresolved_turn_identity"),
                    }
                )
        finally:
            connection.close()
    annotation_by_key: defaultdict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    annotation_by_identity: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    annotation_by_alias: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in annotation_rows:
        key = _gold_key(row)
        annotation_by_key[key].append(row)
        annotation_by_identity[
            (str(row.get("utterance_id") or ""), str(row.get("utterance_role") or ""))
        ].append(row)
        for alias in {row.get("utterance_id", ""), row.get("original_utterance_id", "")}:
            if alias:
                annotation_by_alias[(str(alias), str(row.get("utterance_role") or ""))].append(row)

    selected = {
        str(row[0]): (str(row[1]), "" if row[2] is None else str(row[2]))
        for row in duckdb.sql(
            "SELECT source_row_uid, selected_method, selected_text "
            f"FROM read_parquet('{qpath(FINAL_SELECTION)}')"
        ).fetchall()
    }
    text_col = headers.index("utterance_text") + 1
    provenance_col = len(headers) + 1
    sheet.cell(1, provenance_col, "provenance")
    sheet.cell(1, provenance_col)._style = copy.copy(sheet.cell(1, len(headers))._style)
    sheet.column_dimensions[sheet.cell(1, provenance_col).column_letter].width = 22

    matched_by_row: dict[int, dict[str, str]] = {}
    obsolete_context_rows: list[int] = []
    for row_number in range(2, sheet.max_row + 1):
        values = {
            headers[index - 1]: sheet.cell(row_number, index).value
            for index in range(1, len(headers) + 1)
        }
        key = _gold_key(values)
        key_candidates = annotation_by_key.get(key, [])
        match = None
        if key_candidates:
            source_uid = str(values.get("ssot_source_row_uid") or "")
            if source_uid:
                exact_source = [
                    candidate
                    for candidate in key_candidates
                    if candidate.get("ssot_source_row_uid") == source_uid
                ]
                if len(exact_source) == 1:
                    match = exact_source[0]
            if match is None:
                source_type = str(values.get("utterance_type") or "")
                matching_type = [
                    candidate
                    for candidate in key_candidates
                    if str(candidate.get("utterance_type") or "") == source_type
                ]
                if matching_type:
                    key_candidates = matching_type
                match = min(
                    key_candidates,
                    key=lambda candidate: (
                        str(candidate.get("utterance_type") or "") != "original",
                        int(candidate.get("display_order") or 2**63),
                        str(candidate.get("ssot_source_row_uid") or ""),
                    ),
                )
        if match is None:
            candidates: dict[tuple[str, str], dict[str, str]] = {}
            for alias in {values.get("utterance_id"), values.get("original_utterance_id")}:
                for candidate in annotation_by_alias.get((str(alias or ""), "utterance"), []):
                    candidate_key = (
                        str(candidate.get("ssot_episode_uid") or ""),
                        str(candidate.get("ssot_logical_utterance_uid") or ""),
                    )
                    candidates[candidate_key] = candidate
            same_label = [
                row
                for row in candidates.values()
                if str(row.get("dispute_label") or "") == str(values.get("dispute_label") or "")
            ]
            if len(same_label) == 1:
                match = same_label[0]
            elif len(candidates) == 1:
                match = next(iter(candidates.values()))
        if match is None:
            candidates = annotation_by_identity.get((key[1], key[2]), [])
            if len(candidates) == 1:
                match = candidates[0]
        if match is None:
            source_uid = str(values.get("ssot_source_row_uid") or "")
            if source_uid in integrity_changed_sources or {
                str(values.get("utterance_id") or ""),
                str(values.get("original_utterance_id") or ""),
            }.intersection(integrity_changed_aliases):
                # A historical source occurrence was split or collapsed.  Its
                # old annotation is never copied to a replacement unit.
                obsolete_context_rows.append(row_number)
                invalidated_rows += 1
                continue
            if str(values.get("utterance_role") or "") == "context":
                # Older shells carried a synthetic context scaffold which has
                # no source-occurrence counterpart.  It is neither canonical
                # provenance nor a codable row, so remove it rather than
                # requiring a first context row for the dispute.
                obsolete_context_rows.append(row_number)
                continue
            raise RuntimeError(f"Gold row {row_number} has no unique annotation match: {key}")
        # Source occurrence, rather than logical identity, is the annotation
        # unit.  Do not remove rows that share a canonical logical utterance.
        matched_by_row[row_number] = match

    for row_number in reversed(obsolete_context_rows):
        sheet.delete_rows(row_number)
        matched_by_row = {
            (number - 1 if number > row_number else number): match
            for number, match in matched_by_row.items()
        }

    counts: defaultdict[str, int] = defaultdict(int)
    substantive = context_classified_rows = 0
    rereview_rows = 0
    gold_blockers: list[dict[str, str]] = []
    display_orders_by_row: dict[int, int] = {}
    turn_integrity_part_indexes: dict[int, int] = {}
    for row_number in range(2, sheet.max_row + 1):
        values = {
            headers[index - 1]: sheet.cell(row_number, index).value
            for index in range(1, len(headers) + 1)
        }
        match = matched_by_row[row_number]
        if match.get("display_order") not in (None, ""):
            display_orders_by_row[row_number] = int(match["display_order"])
        if match.get("ssot_turn_integrity_disposition") == "split":
            turn_integrity_part_indexes[row_number] = int(
                match.get("ssot_turn_integrity_part_index") or 0
            )
        substantive += 1
        is_context_classified = bool(
            match.get("ssot_context_node_uid")
            or match.get("ssot_row_provenance") == "context"
            or match.get("utterance_role") == "context"
        )
        if is_context_classified:
            # Context classification remains provenance only.  Its exact
            # source text is codable, but it is not a Method-A/B promotion.
            provenance = "wikidisputes_source"
            sheet.cell(row_number, text_col, match.get("utterance_text") or "")
            context_classified_rows += 1
        elif match.get("ssot_turn_integrity_disposition") == "split":
            # A split unit shares its source-row UID with the replaced
            # cumulative representation.  Its final annotation text—not the
            # source-row selection—is the authoritative Gold projection.
            provenance = "needs_rereview"
            sheet.cell(row_number, text_col, match.get("utterance_text") or "")
        else:
            selection = selected.get(match.get("ssot_source_row_uid", ""))
            provenance = selection[0] if selection else ""
            if provenance not in {"method_a", "method_b", "method_a_fallback"}:
                raise RuntimeError(f"Gold row {row_number} has invalid provenance {provenance!r}")
            # The Gold worksheet is projected from the final annotation
            # units, whose text may include a reviewed overlay repair.  The
            # method selection establishes provenance only; it must not
            # overwrite the final annotation-unit text with a stale variant.
            sheet.cell(row_number, text_col, match.get("utterance_text") or selection[1])
        source_uid = str(match.get("ssot_source_row_uid") or "")
        source_blockers = blocking_decisions.get(source_uid, [])
        if (
            str(match.get("needs_rereview") or "").casefold() == "true"
            or match.get("ssot_turn_integrity_disposition") == "split"
            or source_blockers
        ):
            provenance = "needs_rereview"
            rereview_rows += 1
            for case in source_blockers or [
                {
                    "case_id": str(match.get("ssot_turn_integrity_case_id") or ""),
                    "type": (
                        "split_child_requires_annotation"
                        if match.get("ssot_turn_integrity_disposition") == "split"
                        else str(match.get("ssot_turn_integrity_decision_reason") or "changed_unit")
                    ),
                }
            ]:
                gold_blockers.append(
                    {
                        "utterance_id": str(match.get("utterance_id") or ""),
                        "source_row_uid": source_uid,
                        **case,
                    }
                )
        # Gold shells may carry historical dispute membership.  Once a row
        # has a unique final SSOT match, its annotation-facing identity must
        # always follow that current row rather than the stale shell.
        for field in ("dispute_sequence", "dispute_id", "dispute_label"):
            if field not in match:
                continue
            sheet.cell(row_number, headers.index(field) + 1).value = (
                None if match.get(field) in (None, "") else match.get(field)
            )
        if "escalated" in match:
            sheet.cell(row_number, headers.index("escalated") + 1).value = _escalated_label(
                match["escalated"]
            )
        for field in (
            "utterance_order",
            "substantive_order",
            "utterance_id",
            "original_utterance_id",
            "speaker_id",
            "timestamp",
            "reply_to_utterance_id",
            "reply_to_utterance_id_raw",
            "reply_to_utterance_order",
            "utterance_type",
            "wikipedia_revision_url",
        ):
            if field in headers and field in match:
                value: Any = match.get(field)
                if value not in (None, "") and field in {
                    "utterance_order",
                    "substantive_order",
                    "reply_to_utterance_order",
                }:
                    value = int(value)
                # ``Worksheet.cell(..., value=None)`` leaves an existing value
                # untouched.  Assign through ``Cell.value`` so canonical nulls
                # actually clear stale legacy ranks, timestamps, and IDs.
                sheet.cell(row_number, headers.index(field) + 1).value = (
                    None if value in (None, "") else value
                )
        # The role is annotation-facing rather than canonical provenance.
        sheet.cell(row_number, headers.index("utterance_role") + 1, "utterance")
        sheet.cell(row_number, provenance_col, provenance)
        sheet.cell(row_number, provenance_col)._style = copy.copy(
            sheet.cell(row_number, len(headers))._style
        )
        counts[provenance] += 1

    # Split children have no one-to-one predecessor in Gold.  Append blank
    # annotation shells and explicitly mark them for fresh review; this is the
    # only permitted migration for a 1-to-N historical boundary repair.  A
    # fallback never expands a human sample into the source population.
    existing_unit_ids = {
        str(sheet.cell(row_number, headers.index("utterance_id") + 1).value or "")
        for row_number in range(2, sheet.max_row + 1)
    }
    gold_dispute_ids = {
        str(sheet.cell(row_number, headers.index("dispute_id") + 1).value or "")
        for row_number in range(2, sheet.max_row + 1)
    }
    newly_annotatable = 0
    for match in annotation_rows:
        if (
            match.get("ssot_turn_integrity_disposition") != "split"
            or str(match.get("dispute_id") or "") not in gold_dispute_ids
        ):
            continue
        unit_id = str(match.get("utterance_id") or "")
        if not unit_id or unit_id in existing_unit_ids:
            continue
        row_number = sheet.max_row + 1
        for field, value in match.items():
            if field in headers:
                sheet.cell(row_number, headers.index(field) + 1).value = (
                    _escalated_label(value)
                    if field == "escalated"
                    else (None if value in (None, "") else value)
                )
        sheet.cell(row_number, headers.index("utterance_role") + 1).value = "utterance"
        sheet.cell(row_number, text_col).value = match.get("utterance_text") or ""
        sheet.cell(row_number, provenance_col).value = "needs_rereview"
        gold_blockers.append(
            {
                "utterance_id": unit_id,
                "source_row_uid": str(match.get("ssot_source_row_uid") or ""),
                "case_id": str(match.get("ssot_turn_integrity_case_id") or ""),
                "type": "split_child_requires_annotation",
            }
        )
        sheet.cell(row_number, provenance_col)._style = copy.copy(
            sheet.cell(row_number, len(headers))._style
        )
        existing_unit_ids.add(unit_id)
        turn_integrity_part_indexes[row_number] = int(
            match.get("ssot_turn_integrity_part_index") or 0
        )
        newly_annotatable += 1
        counts["needs_rereview"] += 1

    _sort_gold_rows(
        sheet,
        [*headers, "provenance"],
        display_orders_by_row,
        turn_integrity_part_indexes,
    )
    complete_disputes = _assert_gold_disputes_complete(sheet, headers, annotation_rows)

    # The Gold deliverable is an annotation table, not the source workbook.
    # Keep only the populated annotation sheet even when the input workbook
    # also carries codebooks, rationales, or other supporting tabs.
    for other_sheet in list(workbook.worksheets):
        if other_sheet.title != "Gold_Annotation":
            workbook.remove(other_sheet)

    total = sheet.max_row - 1
    ANNOTATION.mkdir(parents=True, exist_ok=True)
    output_path = ANNOTATION / FINAL_GOLD_NAME
    with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=ANNOTATION, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        workbook.save(temporary)
        if not output_path.exists() or _workbook_values(output_path) != _workbook_values(temporary):
            temporary.replace(output_path)
        else:
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": str(output_path),
        "sha256": _sha256(output_path),
        "rows": total,
        "substantive_rows": substantive,
        "context_rows": 0,
        "context_classified_rows": context_classified_rows,
        "columns": len(headers) + 1,
        "headers": [*headers, "provenance"],
        "provenance": dict(sorted(counts.items())),
        "excluded_discussions": exclusions,
        "complete_current_ssot_disputes": complete_disputes,
        "annotation_readiness": {
            "status": "non_ready" if gold_blockers else "pass",
            "annotation_ready": not gold_blockers,
            "blocking_case_count": len(gold_blockers),
            "blocking_cases": gold_blockers,
        },
        "turn_integrity": {
            "invalidated": invalidated_rows,
            "newly_annotatable": newly_annotatable,
            "needs_rereview": rereview_rows + newly_annotatable,
            "preserved": max(0, total - rereview_rows - newly_annotatable),
        },
    }


def export_annotation_bundle(gold_path: Path) -> dict[str, Any]:
    """Build and validate all supported annotation deliverables."""

    connection = duckdb.connect()
    try:
        setup(connection)
        annotation_report = export_full(connection)
    finally:
        connection.close()
    gold_report = export_annotation_ready_gold(gold_path, Path(annotation_report["annotation_csv"]))
    readiness = _annotation_readiness()
    gold_readiness = gold_report["annotation_readiness"]
    annotation_ready = readiness["annotation_ready"] and gold_readiness["annotation_ready"]
    artifacts = {}
    for path in (
        ANNOTATION / "wikidisputes_llm_annotation_input.csv",
        ANNOTATION / "wikidisputes_annotation_research_key.csv",
        ANNOTATION / FINAL_GOLD_NAME,
    ):
        artifacts[path.name] = {"path": str(path), "sha256": _sha256(path)}
    manifest = {
        # Artifact generation is successful even when review blockers remain.
        # Consumers must use ``annotation_ready`` for handoff decisions rather
        # than treating a completed rebuild as an implicit readiness claim.
        "status": "pass" if annotation_ready else "non_ready",
        "rebuild_completed": True,
        "annotation_ready": annotation_ready,
        "annotation_readiness": readiness,
        "gold_annotation_readiness": gold_readiness,
        "contract_version": "annotation-export-v2-all-source-rows-utterances",
        "validation_decision": _accepted_decision(),
        "annotation": annotation_report,
        "gold": gold_report,
        "artifacts": artifacts,
    }
    atomic_write_json(ANNOTATION / "annotation_manifest.json", manifest)
    return manifest


def _annotation_readiness() -> dict[str, Any]:
    """Report explicit turn-integrity blockers without gating artifact writes."""

    def truthy(value: object) -> bool:
        return str(value).casefold() in {"1", "true", "yes", "y", "blocked"}

    def normalized_blocker(record: Mapping[str, Any]) -> dict[str, Any]:
        blocker = {
            field: str(record.get(field) or "")
            for field in ("source_row_uid", "case_id", "dispute_id")
            if record.get(field) not in (None, "")
        }
        blocker["type"] = str(
            record.get("annotation_blocking_reason")
            or record.get("type")
            or record.get("reason")
            or record.get("decision_reason")
            or record.get("final_disposition")
            or "annotation_blocking"
        )
        return blocker

    blockers: list[dict[str, Any]] = []
    blocker_count = 0
    blocker_types: Counter[str] = Counter()
    source = "none"
    decisions_authoritative = False
    if TURN_INTEGRITY_DECISIONS.exists():
        connection = duckdb.connect()
        try:
            path = qpath(TURN_INTEGRITY_DECISIONS)
            columns = {
                str(row[0])
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{path}')"
                ).fetchall()
            }
            blocking_field = next(
                (field for field in ("annotation_blocking", "blocking") if field in columns),
                None,
            )
            if blocking_field:
                decisions_authoritative = True
                source = "turn_integrity_decisions"
                rows = connection.execute(f"SELECT * FROM read_parquet('{path}')").fetchall()
                names = [str(item[0]) for item in connection.description]
                for values in rows:
                    record = dict(zip(names, values, strict=True))
                    if not truthy(record.get(blocking_field)):
                        continue
                    blocker = normalized_blocker(record)
                    blockers.append(blocker)
                    blocker_types[blocker["type"]] += 1
        finally:
            connection.close()
        blocker_count = len(blockers)

    summary_path = REPORTS / "turn_integrity" / "repair_summary.json"
    if not decisions_authoritative and summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
        payload = summary.get("annotation_blockers")
        if isinstance(payload, Mapping):
            source = "turn_integrity_repair_summary_counts"
            raw_types = payload.get("by_reason") or payload.get("by_type") or {}
            if isinstance(raw_types, Mapping):
                blocker_types.update(
                    {
                        str(reason): int(count)
                        for reason, count in raw_types.items()
                        if int(count) > 0
                    }
                )
            blocker_count = int(payload.get("count") or sum(blocker_types.values()))
            raw_cases = payload.get("cases") or []
            if isinstance(raw_cases, list):
                blockers = [
                    normalized_blocker(blocker)
                    for blocker in raw_cases
                    if isinstance(blocker, Mapping)
                ]
        elif isinstance(payload, list):
            source = "turn_integrity_repair_summary_cases"
            blockers = [
                normalized_blocker(blocker) for blocker in payload if isinstance(blocker, Mapping)
            ]
        if not blockers and isinstance(summary.get("blocking_cases"), list):
            source = "turn_integrity_repair_summary_cases"
            blockers = [
                normalized_blocker(blocker)
                for blocker in summary["blocking_cases"]
                if isinstance(blocker, Mapping)
            ]
        if blockers:
            unique = {
                json.dumps(blocker, sort_keys=True, default=str): blocker for blocker in blockers
            }
            blockers = list(unique.values())
            blocker_count = len(blockers)
            blocker_types = Counter(blocker["type"] for blocker in blockers)

    # Status artifacts can carry dispute-level blockers independently of row
    # decisions.  Use it only when neither decisions nor the summary supplied
    # readiness, so the same blocker cannot be counted through two artifacts.
    if source == "none" and DISPUTE_ANNOTATION_STATUS.exists():
        connection = duckdb.connect()
        try:
            path = qpath(DISPUTE_ANNOTATION_STATUS)
            columns = {
                str(row[0])
                for row in connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{path}')"
                ).fetchall()
            }
            blocking_field = next(
                (field for field in ("annotation_blocking", "blocking") if field in columns),
                None,
            )
            if blocking_field:
                source = "dispute_annotation_status"
                rows = connection.execute(f"SELECT * FROM read_parquet('{path}')").fetchall()
                names = [str(item[0]) for item in connection.description]
                for values in rows:
                    record = dict(zip(names, values, strict=True))
                    if truthy(record.get(blocking_field)):
                        normalized = dict(record)
                        normalized["dispute_id"] = str(
                            record.get("dispute_id")
                            or record.get("episode_uid")
                            or record.get("conversation_id")
                            or ""
                        )
                        normalized["reason"] = str(
                            record.get("annotation_blocking_reason")
                            or record.get("exclusion_reason")
                            or "annotation_blocking"
                        )
                        blocker = normalized_blocker(normalized)
                        blockers.append(blocker)
                        blocker_types[blocker["type"]] += 1
        finally:
            connection.close()
        blocker_count = len(blockers)

    annotation_ready = blocker_count == 0
    return {
        "status": "pass" if annotation_ready else "non_ready",
        "rebuild_completed": True,
        "annotation_ready": annotation_ready,
        "blocking_case_count": blocker_count,
        "blocking_by_type": dict(sorted(blocker_types.items())),
        "blocking_cases": blockers,
        "source": source,
    }
