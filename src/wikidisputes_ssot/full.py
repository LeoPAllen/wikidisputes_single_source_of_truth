from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .constants import (
    IDENTITY_VERSION,
    JOIN_CONTRACT_VERSION,
    REPRESENTATION_VERSION,
    SCHEMA_VERSION,
)
from .cross_label import materialize_cross_label_reconciliation
from .hashing import canonical_json_hash, sha256_bytes
from .io import (
    atomic_link_or_copy,
    atomic_parquet,
    atomic_write_json,
    file_descriptor,
    table_from_union_pylist,
)
from .representations import extract_links, extract_signature_evidence


def _uid(namespace: str, *parts: Any) -> str:
    return f"{namespace}:v1:" + canonical_json_hash(list(parts))


def _iso_from_unix(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return dt.datetime.fromtimestamp(float(value), tz=dt.UTC).isoformat()


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(dt.UTC)
    except ValueError:
        return None


def _id_parts(value: Any) -> tuple[int, int, int]:
    maximum = 2**63 - 1
    if not isinstance(value, str):
        return maximum, maximum, maximum
    result: list[int] = []
    for part in value.split(".")[:3]:
        try:
            result.append(int(part))
        except ValueError:
            result.append(maximum)
    return tuple((result + [maximum] * 3)[:3])  # type: ignore[return-value]


def _write(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        table = table_from_union_pylist(rows)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        raise RuntimeError(f"cannot materialize {path.name}: {exc}") from exc
    atomic_parquet(path, table)
    return {**file_descriptor(path), "rows": len(rows)}


def _append_only_registry(
    existing: list[dict[str, Any]], derived: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_uid = {str(row["registry_entry_uid"]): row for row in existing}
    for row in derived:
        by_uid.setdefault(str(row["registry_entry_uid"]), row)
    return list(by_uid.values())


def _source_logical_anchor(row: dict[str, Any]) -> str:
    original = row.get("wikidisputes_original_id_exact")
    current = row.get("wikidisputes_id_exact")
    if isinstance(original, str) and original:
        return original
    if row.get("wikidisputes_type_exact") == "original" and isinstance(current, str) and current:
        return current
    return "fallback:" + str(row["source_row_uid"])


def _is_context(row: dict[str, Any]) -> bool:
    return bool(
        row.get("source_row_index") == 0
        and row.get("wikidisputes_id_exact")
        and row.get("wikidisputes_id_exact") == row.get("wikidisputes_conv_id_exact")
        and row.get("wikidisputes_reply_to_exact") is None
    )


def _wikiconv_lifecycle(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten WikiConv's nested action history without losing the parent row."""
    meta = json.loads(row["meta_json_canonical"])
    original = meta.get("original")
    actions: list[dict[str, Any]] = []
    if isinstance(original, dict):
        actions.append({"action_type": "creation", **original})
    else:
        actions.append(
            {
                "action_type": "creation",
                "id": row.get("ancestor_id_exact") or row.get("wikiconv_id_exact"),
                "speaker": row.get("wikiconv_speaker_exact"),
                "root": row.get("conversation_id_exact"),
                "reply_to": row.get("wikiconv_reply_to_exact"),
                "timestamp": row.get("wikiconv_timestamp_unix"),
                "text": row.get("wikiconv_text_exact"),
                "meta_dict": meta,
            }
        )
    for field, action_type in (
        ("modification", "modification"),
        ("deletion", "deletion"),
        ("restoration", "restoration"),
    ):
        values = meta.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                actions.append({"action_type": action_type, **value})
    return actions


def _speaker_exact(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return str(value["id"])
    return None


def _revision_id(value: Any, action_id: Any) -> int | None:
    candidate = value
    if candidate is None:
        candidate = _id_parts(action_id)[0]
    try:
        numeric = int(candidate)
    except (TypeError, ValueError):
        return None
    return numeric if numeric < 2**63 - 1 else None


def _episode_membership(
    output_root: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    episodes = pq.read_table(output_root / "silver" / "dispute_episodes.parquet").to_pylist()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        grouped[str(episode["source_conversation_id_exact"])].append(episode)
    return grouped, episodes


def materialize_full_rehydrated(output_root: Path) -> dict[str, Any]:
    """Build the full selected-conversation logical/action bundle.

    This consumes only a *completed* annual WikiConv merge. It never promotes a
    partial scan to conversational completeness.
    """
    enumeration_report = json.loads(
        (output_root / "reports" / "conversation_enumeration.json").read_text(encoding="utf-8")
    )
    if enumeration_report["status"] not in {"complete", "gaps_or_conflicts"}:
        raise RuntimeError("WikiConv enumeration has no terminal coverage status")

    source = pq.read_table(
        output_root / "canonical" / "wikidisputes_source_projection.parquet"
    ).to_pylist()
    wikiconv_all = pq.read_table(
        output_root / "silver" / "wikiconv_selected_rows.parquet"
    ).to_pylist()

    # Preserve conflicting action observations; collapse only byte-identical
    # annual repeats of one WikiConv action identity.
    wc_by_observation: dict[tuple[str, str], dict[str, Any]] = {}
    for row in wikiconv_all:
        key = (str(row["wikiconv_id_exact"]), str(row["source_record_sha256"]))
        previous = wc_by_observation.get(key)
        if previous is None or int(row["corpus_year"]) < int(previous["corpus_year"]):
            wc_by_observation[key] = row
    wikiconv = list(wc_by_observation.values())

    wc_context_by_uid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    wc_context_alias_to_uid: dict[str, set[str]] = defaultdict(set)
    wc_utterance_rows: list[dict[str, Any]] = []
    for row in wikiconv:
        if row.get("is_section_header") is True:
            anchor = str(row.get("ancestor_id_exact") or row["wikiconv_id_exact"])
            context_uid = f"wikiconv-context:{anchor}"
            wc_context_by_uid[context_uid].append(row)
            for value in (row.get("wikiconv_id_exact"), row.get("ancestor_id_exact")):
                if value:
                    wc_context_alias_to_uid[str(value)].add(context_uid)
            for lifecycle in _wikiconv_lifecycle(row):
                if lifecycle.get("id"):
                    wc_context_alias_to_uid[str(lifecycle["id"])].add(context_uid)
        else:
            wc_utterance_rows.append(row)
    observed_conversation_ids = {str(row["conversation_id_exact"]) for row in wikiconv}
    source_to_context: dict[str, str] = {}
    context_source_uids: set[str] = set()
    for row in source:
        candidates: set[str] = set()
        for value in (
            row.get("wikidisputes_id_exact"),
            row.get("wikidisputes_original_id_exact"),
        ):
            if value:
                candidates.update(wc_context_alias_to_uid.get(str(value), set()))
        if len(candidates) == 1:
            context_uid = next(iter(candidates))
            source_uid = str(row["source_row_uid"])
            context_source_uids.add(source_uid)
            source_to_context[source_uid] = context_uid
        elif (
            _is_context(row)
            and str(row.get("wikidisputes_conv_id_exact")) not in observed_conversation_ids
        ):
            source_uid = str(row["source_row_uid"])
            context_uid = _uid(
                "wdcontext",
                "source_only_candidate",
                row.get("wikidisputes_conv_id_exact"),
                source_uid,
            )
            context_source_uids.add(source_uid)
            source_to_context[source_uid] = context_uid
    source_alias_to_anchors: dict[str, set[str]] = defaultdict(set)
    source_by_anchor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        if row["source_row_uid"] in context_source_uids:
            continue
        anchor = _source_logical_anchor(row)
        source_by_anchor[anchor].append(row)
        for alias in (
            row.get("wikidisputes_id_exact"),
            row.get("wikidisputes_original_id_exact"),
        ):
            if alias:
                source_alias_to_anchors[str(alias)].add(anchor)

    wc_by_logical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    wc_alias_to_logical: dict[str, set[str]] = defaultdict(set)
    for row in wc_utterance_rows:
        anchor = str(row.get("ancestor_id_exact") or row["wikiconv_id_exact"])
        logical_uid = f"wikiconv:{anchor}"
        wc_by_logical[logical_uid].append(row)
        for alias in (
            row.get("wikiconv_id_exact"),
            row.get("ancestor_id_exact"),
            row.get("parent_id_exact"),
        ):
            if alias:
                wc_alias_to_logical[str(alias)].add(logical_uid)
        for lifecycle in _wikiconv_lifecycle(row):
            if lifecycle.get("id"):
                wc_alias_to_logical[str(lifecycle["id"])].add(logical_uid)

    # Resolve every source anchor to authoritative WikiConv identity when unique.
    source_anchor_to_logical: dict[str, str] = {}
    source_resolution: dict[str, str] = {}
    source_resolution_candidates: dict[str, list[str]] = {}
    for anchor, rows in source_by_anchor.items():
        candidates: set[str] = set()
        candidate_aliases: set[str] = {anchor}
        for row in rows:
            candidate_aliases.update(
                str(value)
                for value in (
                    row.get("wikidisputes_id_exact"),
                    row.get("wikidisputes_original_id_exact"),
                )
                if value
            )
        for alias in candidate_aliases:
            candidates.update(wc_alias_to_logical.get(alias, set()))
        source_resolution_candidates[anchor] = sorted(candidates)
        if len(candidates) == 1:
            source_anchor_to_logical[anchor] = next(iter(candidates))
            source_resolution[anchor] = "unique_wikiconv_alias"
        elif not anchor.startswith("fallback:"):
            source_anchor_to_logical[anchor] = f"wikiconv:{anchor}"
            source_resolution[anchor] = (
                "ambiguous_wikiconv_alias_fallback"
                if len(candidates) > 1
                else "source_authoritative_creation_alias"
            )
        else:
            source_anchor_to_logical[anchor] = "wdutt:fallback:v1:" + canonical_json_hash(
                ["immutable-source-row", anchor.removeprefix("fallback:")]
            )
            source_resolution[anchor] = "unresolved_action_location_fallback"

    source_by_logical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_anchors_by_logical: dict[str, list[str]] = defaultdict(list)
    for anchor, rows in source_by_anchor.items():
        resolved_logical = source_anchor_to_logical[anchor]
        source_by_logical[resolved_logical].extend(rows)
        source_anchors_by_logical[resolved_logical].append(anchor)

    all_logical_uids = sorted(set(wc_by_logical) | set(source_by_logical))
    episode_by_conversation, episode_rows = _episode_membership(output_root)
    outcome_rows = pq.read_table(output_root / "silver" / "outcomes.parquet").to_pylist()
    outcomes_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcome_rows:
        outcomes_by_episode[str(outcome["episode_uid"])].append(outcome)

    utterances: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    context_actions: list[dict[str, Any]] = []
    context_representations: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    existing_registry_path = output_root / "silver" / "identity_registry.parquet"
    existing_registry = (
        pq.read_table(existing_registry_path).to_pylist() if existing_registry_path.exists() else []
    )
    registry: list[dict[str, Any]] = []
    actor_rows: list[dict[str, Any]] = []
    signatures: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []
    source_to_logical: dict[str, str] = {}
    source_action_resolution: dict[str, dict[str, Any]] = {}
    creation_by_logical: dict[str, dict[str, Any]] = {}
    identity_method_by_logical: dict[str, str] = {}

    for logical_uid in all_logical_uids:
        wc_rows = sorted(
            wc_by_logical.get(logical_uid, []),
            key=lambda row: (int(row["corpus_year"]), int(row["source_line_index"])),
        )
        source_rows = sorted(
            source_by_logical.get(logical_uid, []), key=lambda row: row["source_order"]
        )
        for row in source_rows:
            source_to_logical[str(row["source_row_uid"])] = logical_uid
        representative_wc = wc_rows[0] if wc_rows else None
        lifecycle_actions: list[dict[str, Any]] = []
        seen_lifecycle: set[tuple[str, str, str, str]] = set()
        for wc_row in wc_rows:
            for lifecycle in _wikiconv_lifecycle(wc_row):
                lifecycle_key = (
                    str(lifecycle.get("id")),
                    str(lifecycle.get("action_type")),
                    str(lifecycle.get("timestamp")),
                    canonical_json_hash(lifecycle),
                )
                if lifecycle_key in seen_lifecycle:
                    continue
                seen_lifecycle.add(lifecycle_key)
                lifecycle_actions.append(
                    {**lifecycle, "wikiconv_source_row_uid": wc_row["wikiconv_source_row_uid"]}
                )
        creation_action = next(
            (row for row in lifecycle_actions if row["action_type"] == "creation"), None
        )
        original_source = next(
            (row for row in source_rows if row.get("wikidisputes_type_exact") == "original"),
            None,
        )
        conversation_id = str(
            (representative_wc or {}).get("conversation_id_exact")
            or (original_source or source_rows[0]).get("wikidisputes_conv_id_exact")
        )
        created_at = (
            _iso_from_unix(creation_action.get("timestamp"))
            if creation_action
            else (original_source or {}).get("wikidisputes_time")
        )
        creation_id = (
            str(creation_action.get("id"))
            if creation_action
            else (original_source or {}).get("wikidisputes_original_id_exact")
            or (original_source or {}).get("wikidisputes_id_exact")
        )
        creation_by_logical[logical_uid] = {
            "conversation_id": conversation_id,
            "created_at": created_at,
            "creation_id": creation_id,
            "source_order": min((row["source_order"] for row in source_rows), default=2**63 - 1),
        }
        source_anchors = sorted(source_anchors_by_logical.get(logical_uid, []))
        method = (
            "wikiconv_ancestor_id"
            if representative_wc
            else source_resolution.get(source_anchors[0], "source_alias_fallback")
            if source_anchors
            else "source_alias_fallback"
        )
        identity_method_by_logical[logical_uid] = method
        resolution_candidates = sorted(
            {
                candidate
                for anchor in source_anchors
                for candidate in source_resolution_candidates.get(anchor, [])
            }
        )
        registry.append(
            {
                "registry_entry_uid": _uid("wdregistry", logical_uid, IDENTITY_VERSION),
                "issued_uid": logical_uid,
                "entity_kind": "logical_utterance",
                "derivation_method": method,
                "selected_anchor": creation_id,
                "candidate_anchors_json": json.dumps(
                    resolution_candidates
                    or sorted(
                        {
                            str(value)
                            for row in wc_rows + source_rows
                            for value in (
                                row.get("wikiconv_id_exact"),
                                row.get("ancestor_id_exact"),
                                row.get("wikidisputes_id_exact"),
                                row.get("wikidisputes_original_id_exact"),
                            )
                            if value
                        }
                    ),
                    ensure_ascii=False,
                ),
                "confidence": "high"
                if representative_wc
                else "ambiguous"
                if resolution_candidates
                else "low",
                "adjudication_status": (
                    "not_required"
                    if representative_wc
                    else "pending_competing_candidates"
                    if resolution_candidates
                    else "pending"
                ),
                "algorithm_version": IDENTITY_VERSION,
                "effective_version": SCHEMA_VERSION,
                "registry_status": "active",
            }
        )

        wc_action_by_id: dict[str, dict[str, Any]] = {}
        if representative_wc:
            for lifecycle_index, lifecycle in enumerate(lifecycle_actions):
                action_id = str(lifecycle.get("id"))
                action_type = str(lifecycle["action_type"])
                action_uid = _uid("wdaction", logical_uid, action_type, action_id, lifecycle_index)
                version_uid = _uid("wdversion", action_uid)
                meta_dict = lifecycle.get("meta_dict")
                nested_meta = meta_dict if isinstance(meta_dict, dict) else {}
                action_row = {
                    "action_uid": action_uid,
                    "version_uid": version_uid,
                    "logical_utterance_uid": logical_uid,
                    "source_row_uid": None,
                    "source_row_uids_json": "[]",
                    "wikiconv_source_row_uid": lifecycle["wikiconv_source_row_uid"],
                    "action_type": action_type,
                    "action_id_exact": action_id,
                    "raw_timestamp": _iso_from_unix(lifecycle.get("timestamp")),
                    "revision_id": _revision_id(nested_meta.get("rev_id"), lifecycle.get("id")),
                    "parent_action_id_exact": nested_meta.get("parent_id"),
                    "raw_action_json_canonical": json.dumps(
                        lifecycle, ensure_ascii=False, sort_keys=True, default=str
                    ),
                    "recovery_status": "recovered_from_pinned_wikiconv",
                    "recovery_method": "wikiconv_nested_lifecycle",
                    "schema_version": SCHEMA_VERSION,
                }
                actions.append(action_row)
                wc_action_by_id[action_id] = action_row
                action_text = lifecycle.get("text")
                action_encoded = (action_text or "").encode("utf-8")
                representations.append(
                    {
                        "representation_uid": _uid(
                            "wdrepr", version_uid, "wikiconv_action_text_exact"
                        ),
                        "logical_utterance_uid": logical_uid,
                        "version_uid": version_uid,
                        "source_row_uid": None,
                        "representation_kind": "wikiconv_action_text_exact",
                        "content_sha256": sha256_bytes(action_encoded),
                        "byte_length": len(action_encoded),
                        "encoding": "utf-8",
                        "mime_type": "text/plain",
                        "content_inline": action_text,
                        "blob_path": None,
                        "source_revision_id": (
                            str(action_row["revision_id"])
                            if action_row.get("revision_id") is not None
                            else None
                        ),
                        "extraction_method": "pinned_wikiconv_nested_action",
                        "extraction_version": "1.0.0",
                        "availability_status": "deleted_at_action"
                        if action_type == "deletion"
                        else "available",
                        "leakage_class": "action_time_state",
                        "available_at": action_row["raw_timestamp"],
                        "confidence": "exact_wikiconv_action_field",
                        "representation_version": REPRESENTATION_VERSION,
                    }
                )
            text = representative_wc.get("wikiconv_text_exact")
            encoded = (text or "").encode("utf-8")
            current_action = wc_action_by_id.get(str(representative_wc["wikiconv_id_exact"]))
            if current_action is None:
                current_action = wc_action_by_id[str(creation_id)]
            version_uid = str(current_action["version_uid"])
            representation_uid = _uid("wdrepr", version_uid, "wikiconv_final_text")
            representations.append(
                {
                    "representation_uid": representation_uid,
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": None,
                    "representation_kind": "wikiconv_final_text_exact",
                    "content_sha256": sha256_bytes(encoded),
                    "byte_length": len(encoded),
                    "encoding": "utf-8",
                    "mime_type": "text/plain",
                    "content_inline": text,
                    "blob_path": None,
                    "source_revision_id": (
                        str(current_action["revision_id"])
                        if current_action.get("revision_id") is not None
                        else None
                    ),
                    "extraction_method": "pinned_wikiconv_json_decode",
                    "extraction_version": "1.0.0",
                    "availability_status": "available" if text is not None else "unknown",
                    "leakage_class": "final_state_not_predictor_safe",
                    "available_at": current_action.get("raw_timestamp"),
                    "confidence": "exact_wikiconv_field",
                    "representation_version": REPRESENTATION_VERSION,
                }
            )
            signature = extract_signature_evidence(text or "")
            signature_uid = _uid("wdsignature", version_uid, signature["signature_status"])
            signatures.append(
                {
                    "signature_uid": signature_uid,
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    **signature,
                    "signature_html_reconstructed": None,
                    "parsed_signature_timestamp": None,
                    "actor_match_status": "not_testable_without_revision_actor",
                    "evidence_pointer": f"representation:{representation_uid}",
                    "confidence": "explicit_pattern"
                    if signature["raw_signature_wikitext"]
                    else "none",
                }
            )
            for link in extract_links(
                text or "", logical_utterance_uid=logical_uid, version_uid=version_uid
            ):
                links.append(
                    {
                        **link.__dict__,
                        "logical_utterance_uid": logical_uid,
                        "version_uid": version_uid,
                        "source_representation_uid": representation_uid,
                        "present_in_wikidisputes_text": None,
                        "recovered_from_revision": False,
                        "evidence_pointer": f"representation:{representation_uid}",
                        "confidence": "explicit_target_only",
                        "ambiguity": None,
                    }
                )
            actor_rows.append(
                {
                    "author_actor_uid": _uid("wdauthor", version_uid),
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": None,
                    "wikidisputes_user_exact": None,
                    "wikiconv_speaker_exact": _speaker_exact(
                        (creation_action or {}).get("speaker")
                    ),
                    "revision_actor_name_exact": None,
                    "revision_actor_user_id": None,
                    "identity_status": "wikiconv_speaker_only",
                    "resolved_identity": None,
                    "resolution_method": None,
                    "confidence": "unresolved",
                }
            )

        for row in source_rows:
            action_type = {
                "original": "creation",
                "modification": "modification",
                "restoration": "restoration",
                "deletion": "deletion",
            }.get(str(row.get("wikidisputes_type_exact")), "unknown")
            source_action_id = str(row.get("wikidisputes_id_exact"))
            matched_action = wc_action_by_id.get(source_action_id)
            if matched_action:
                source_uids = json.loads(matched_action["source_row_uids_json"])
                source_uids.append(row["source_row_uid"])
                matched_action["source_row_uids_json"] = json.dumps(sorted(set(source_uids)))
                source_action_resolution[str(row["source_row_uid"])] = matched_action
                version_uid = str(matched_action["version_uid"])
            else:
                action_uid = _uid("wdaction", row["source_row_uid"])
                version_uid = _uid("wdversion", action_uid)
                source_action = {
                    "action_uid": action_uid,
                    "version_uid": version_uid,
                    "logical_utterance_uid": logical_uid,
                    "source_row_uid": row["source_row_uid"],
                    "source_row_uids_json": json.dumps([row["source_row_uid"]]),
                    "wikiconv_source_row_uid": None,
                    "action_type": action_type,
                    "action_id_exact": row.get("wikidisputes_id_exact"),
                    "raw_timestamp": row.get("wikidisputes_time"),
                    "revision_id": _revision_id(None, row.get("wikidisputes_id_exact")),
                    "parent_action_id_exact": row.get("wikidisputes_original_id_exact"),
                    "raw_action_json_canonical": row["source_record_json_exact"],
                    "recovery_status": "source_projection_action",
                    "recovery_method": "wikidisputes_projection",
                    "schema_version": SCHEMA_VERSION,
                }
                actions.append(source_action)
                source_action_resolution[str(row["source_row_uid"])] = source_action
            text = row.get("wikidisputes_text_exact")
            encoded = (text or "").encode("utf-8")
            source_representation_uid = _uid(
                "wdrepr", version_uid, "wikidisputes_text_exact", row["source_row_uid"]
            )
            representations.append(
                {
                    "representation_uid": source_representation_uid,
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": row["source_row_uid"],
                    "representation_kind": "wikidisputes_text_exact",
                    "content_sha256": sha256_bytes(encoded),
                    "byte_length": len(encoded),
                    "encoding": "utf-8",
                    "mime_type": "text/plain",
                    "content_inline": text,
                    "blob_path": None,
                    "source_revision_id": None,
                    "extraction_method": "json_decode_without_normalization",
                    "extraction_version": "1.0.0",
                    "availability_status": "available" if text is not None else "unknown",
                    "leakage_class": "source_available",
                    "available_at": row.get("wikidisputes_time"),
                    "confidence": "exact_source_evidence",
                    "representation_version": REPRESENTATION_VERSION,
                }
            )
            source_signature = extract_signature_evidence(text or "")
            signatures.append(
                {
                    "signature_uid": _uid(
                        "wdsignature", version_uid, row["source_row_uid"], "source"
                    ),
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    **source_signature,
                    "signature_html_reconstructed": None,
                    "parsed_signature_timestamp": None,
                    "actor_match_status": "not_testable_from_source_projection",
                    "evidence_pointer": f"representation:{source_representation_uid}",
                    "confidence": "explicit_pattern"
                    if source_signature["raw_signature_wikitext"]
                    else "none",
                }
            )
            for source_link in extract_links(
                text or "", logical_utterance_uid=logical_uid, version_uid=version_uid
            ):
                links.append(
                    {
                        **source_link.__dict__,
                        "logical_utterance_uid": logical_uid,
                        "version_uid": version_uid,
                        "source_representation_uid": source_representation_uid,
                        "present_in_wikidisputes_text": True,
                        "recovered_from_revision": False,
                        "evidence_pointer": f"representation:{source_representation_uid}",
                        "confidence": "explicit_target_only",
                        "ambiguity": None,
                    }
                )
            actor_rows.append(
                {
                    "author_actor_uid": _uid("wdauthor", row["source_row_uid"]),
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": row["source_row_uid"],
                    "wikidisputes_user_exact": row.get("wikidisputes_user_exact"),
                    "wikiconv_speaker_exact": None,
                    "revision_actor_name_exact": None,
                    "revision_actor_user_id": None,
                    "identity_status": "source_username_only",
                    "resolved_identity": None,
                    "resolution_method": None,
                    "confidence": "unresolved",
                }
            )

        for namespace, rows, columns in (
            (
                "wikiconv",
                wc_rows,
                ("wikiconv_id_exact", "ancestor_id_exact", "parent_id_exact"),
            ),
            (
                "wikidisputes",
                source_rows,
                (
                    "wikidisputes_id_exact",
                    "wikidisputes_original_id_exact",
                    "wikidisputes_reply_to_exact",
                ),
            ),
        ):
            for row in rows:
                occurrence = row.get("wikiconv_source_row_uid") or row.get("source_row_uid")
                for column in columns:
                    value = row.get(column)
                    if value is None:
                        continue
                    aliases.append(
                        {
                            "alias_uid": _uid("wdalias", occurrence, namespace, column, value),
                            "alias_namespace": f"{namespace}_{column}",
                            "alias_value_exact": value,
                            "source_row_uid": row.get("source_row_uid"),
                            "wikiconv_source_row_uid": row.get("wikiconv_source_row_uid"),
                            "entity_kind": "logical_utterance",
                            "resolved_entity_uid": logical_uid,
                            "resolution_status": "resolved"
                            if column not in {"wikidisputes_reply_to_exact", "parent_id_exact"}
                            else "target_alias_observed",
                            "validity_status": "observed",
                            "evidence_pointer": f"source_occurrence:{occurrence}",
                            "schema_version": SCHEMA_VERSION,
                        }
                    )
        for action_id, action_row in wc_action_by_id.items():
            aliases.append(
                {
                    "alias_uid": _uid(
                        "wdalias", action_row["action_uid"], "wikiconv_action_id", action_id
                    ),
                    "alias_namespace": "wikiconv_action_id",
                    "alias_value_exact": action_id,
                    "source_row_uid": None,
                    "wikiconv_source_row_uid": action_row["wikiconv_source_row_uid"],
                    "entity_kind": "utterance_action",
                    "resolved_entity_uid": logical_uid,
                    "resolved_action_uid": action_row["action_uid"],
                    "resolution_status": "resolved",
                    "validity_status": "observed",
                    "evidence_pointer": (
                        f"wikiconv_source_row:{action_row['wikiconv_source_row_uid']}"
                    ),
                    "schema_version": SCHEMA_VERSION,
                }
            )

    # Canonical order is time, numeric creation revision/position, source order,
    # stable UID. Equal timestamps share a simultaneity group.
    order_by_logical: dict[str, int] = {}
    simultaneity_by_logical: dict[str, str] = {}
    grouped_logical: dict[str, list[str]] = defaultdict(list)
    for logical_uid, creation in creation_by_logical.items():
        grouped_logical[str(creation["conversation_id"])].append(logical_uid)
    for conversation_id, logical_uids in grouped_logical.items():
        logical_uids.sort(
            key=lambda uid: (
                _parse_iso(creation_by_logical[uid]["created_at"])
                or dt.datetime.max.replace(tzinfo=dt.UTC),
                _id_parts(creation_by_logical[uid]["creation_id"]),
                creation_by_logical[uid]["source_order"],
                uid,
            )
        )
        for order, logical_uid in enumerate(logical_uids, start=1):
            order_by_logical[logical_uid] = order
            timestamp = creation_by_logical[logical_uid]["created_at"]
            simultaneity_by_logical[logical_uid] = _uid(
                "wdsimultaneity", conversation_id, timestamp
            )

    metadata_by_conversation: dict[str, dict[str, Any]] = {}
    metadata_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    metadata_path = output_root / "silver" / "wikiconv_conversation_metadata.parquet"
    if metadata_path.exists():
        for observation in pq.read_table(metadata_path).to_pylist():
            conversation_id = str(observation["conversation_id_exact"])
            metadata_by_conversation.setdefault(conversation_id, observation)
            metadata_candidates[conversation_id].append(observation)
    for conversation_id, candidates in metadata_candidates.items():
        hashes = sorted({str(row["metadata_sha256"]) for row in candidates})
        if len(hashes) > 1:
            quality.append(
                {
                    "quality_flag_uid": _uid(
                        "wdquality", conversation_id, "conversation_metadata_conflict"
                    ),
                    "entity_uid": "wikiconv-conversation:" + conversation_id,
                    "flag_code": "conversation_metadata_conflict",
                    "severity": "warning",
                    "evidence_pointer": json.dumps(hashes),
                }
            )
    disputes = pq.read_table(output_root / "silver" / "disputes.parquet").to_pylist()
    dispute_by_uid = {str(row["dispute_uid"]): row for row in disputes}
    speakers_by_conversation: dict[str, set[str]] = defaultdict(set)
    for logical_uid, rows in wc_by_logical.items():
        conversation_id = str(creation_by_logical[logical_uid]["conversation_id"])
        for row in rows:
            speaker = _speaker_exact(row.get("wikiconv_speaker_exact"))
            if speaker:
                speakers_by_conversation[conversation_id].add(speaker.casefold())
    for episode in episode_rows:
        conversation_id = str(episode["source_conversation_id_exact"])
        observation = metadata_by_conversation.get(conversation_id)
        if observation:
            parsed = json.loads(str(observation["metadata_json_exact"]))
            metadata = parsed.get("meta", {}) if isinstance(parsed, dict) else {}
            episode["page_id_exact"] = metadata.get("page_id")
            wiki_title = metadata.get("page_title")
            source_title = episode.get("title_at_event_exact")
            title_matches = (
                isinstance(wiki_title, str)
                and isinstance(source_title, str)
                and wiki_title.replace("_", " ").casefold()
                == source_title.replace("_", " ").casefold()
            )
            episode["page_identity_match_status"] = (
                "page_id_observed_title_exact"
                if title_matches
                else "page_id_observed_title_differs"
            )
        else:
            episode["page_identity_match_status"] = "conversation_metadata_unavailable"
        logical_ids = grouped_logical.get(conversation_id, [])
        creation_times = [
            parsed
            for uid in logical_ids
            if (parsed := _parse_iso(creation_by_logical[uid]["created_at"])) is not None
        ]
        thread_start = min(creation_times) if creation_times else None
        episode["thread_start_at"] = thread_start.isoformat() if thread_start else None
        dispute = dispute_by_uid.get(str(episode["dispute_uid"]), {})
        dispute_metadata = json.loads(str(dispute.get("dispute_json_canonical", "{}")))
        source_participants: set[str] = set()
        users = dispute_metadata.get("users")
        if isinstance(users, list):
            source_participants.update(str(value).casefold() for value in users if value)
        for side in ("before", "after"):
            side_value = dispute_metadata.get(side)
            if isinstance(side_value, dict) and side_value.get("username"):
                source_participants.add(str(side_value["username"]).casefold())
        overlap = source_participants & speakers_by_conversation.get(conversation_id, set())
        episode["participant_overlap_count"] = len(overlap)
        episode["participant_overlap_status"] = (
            "observed_overlap"
            if overlap
            else "observed_none"
            if source_participants
            else "source_participants_not_observed"
        )
        event_time = _parse_iso(
            dispute_metadata.get("timestamp") or dispute_metadata.get("start_timestamp")
        )
        episode["temporal_distance_seconds"] = (
            (event_time - thread_start).total_seconds()
            if event_time is not None and thread_start is not None
            else None
        )
        episode["temporal_alignment_status"] = (
            "computed" if event_time is not None and thread_start is not None else "not_computed"
        )
        if (
            episode.get("page_identity_match_status") == "page_id_observed_title_exact"
            and logical_ids
        ):
            episode["alignment_status"] = "probable"
            episode["alignment_reasons"] = (
                "selected conversation observed; stable page ID and title match; "
                "section/link/move evidence not fully hydrated"
            )

    action_by_logical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        action_by_logical[str(action["logical_utterance_uid"])].append(action)
    for logical_uid, action_rows in action_by_logical.items():
        actions_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        children_by_parent: dict[str, list[str]] = defaultdict(list)
        for action in action_rows:
            action_id = str(action.get("action_id_exact"))
            actions_by_id[action_id].append(action)
            parent = action.get("parent_action_id_exact")
            if parent:
                children_by_parent[str(parent)].append(action_id)
        for action in action_rows:
            parent = action.get("parent_action_id_exact")
            if parent and str(parent) not in actions_by_id:
                quality.append(
                    {
                        "quality_flag_uid": _uid(
                            "wdquality", action["action_uid"], "missing_lifecycle_parent"
                        ),
                        "entity_uid": action["action_uid"],
                        "flag_code": "missing_lifecycle_parent",
                        "severity": "warning",
                        "evidence_pointer": f"parent_action_id:{parent}",
                    }
                )
        for parent, child_ids in children_by_parent.items():
            if len(set(child_ids)) > 1:
                quality.append(
                    {
                        "quality_flag_uid": _uid(
                            "wdquality", logical_uid, "concurrent_lifecycle_branch", parent
                        ),
                        "entity_uid": logical_uid,
                        "flag_code": "concurrent_lifecycle_branch",
                        "severity": "warning",
                        "evidence_pointer": json.dumps(sorted(set(child_ids))),
                    }
                )
        graph = {
            action_id: str(rows[0].get("parent_action_id_exact"))
            for action_id, rows in actions_by_id.items()
            if rows[0].get("parent_action_id_exact")
            and str(rows[0].get("parent_action_id_exact")) in actions_by_id
        }
        for start in graph:
            seen: set[str] = set()
            node: str | None = start
            while node in graph:
                if node in seen:
                    quality.append(
                        {
                            "quality_flag_uid": _uid(
                                "wdquality", logical_uid, "lifecycle_cycle", start
                            ),
                            "entity_uid": logical_uid,
                            "flag_code": "lifecycle_cycle",
                            "severity": "error",
                            "evidence_pointer": json.dumps(sorted(seen)),
                        }
                    )
                    break
                seen.add(node)
                node = graph.get(node)
        ordered_actions = sorted(
            action_rows,
            key=lambda row: (
                _parse_iso(row.get("raw_timestamp")) or dt.datetime.max.replace(tzinfo=dt.UTC),
                str(row["action_uid"]),
            ),
        )
        deleted = False
        for action in ordered_actions:
            if action["action_type"] == "deletion":
                if deleted:
                    quality.append(
                        {
                            "quality_flag_uid": _uid(
                                "wdquality", action["action_uid"], "repeated_deletion"
                            ),
                            "entity_uid": action["action_uid"],
                            "flag_code": "repeated_deletion_without_restoration",
                            "severity": "warning",
                            "evidence_pointer": f"action:{action['action_uid']}",
                        }
                    )
                deleted = True
            elif action["action_type"] == "restoration":
                if not deleted:
                    quality.append(
                        {
                            "quality_flag_uid": _uid(
                                "wdquality", action["action_uid"], "restoration_without_deletion"
                            ),
                            "entity_uid": action["action_uid"],
                            "flag_code": "restoration_without_observed_deletion",
                            "severity": "warning",
                            "evidence_pointer": f"action:{action['action_uid']}",
                        }
                    )
                deleted = False
    representation_by_logical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for representation in representations:
        representation_by_logical[str(representation["logical_utterance_uid"])].append(
            representation
        )

    for logical_uid in all_logical_uids:
        wc_rows = wc_by_logical.get(logical_uid, [])
        source_rows = source_by_logical.get(logical_uid, [])
        creation = creation_by_logical[logical_uid]
        action_rows = action_by_logical[logical_uid]
        types = [str(row["action_type"]) for row in action_rows]
        episodes = episode_by_conversation.get(str(creation["conversation_id"]), [])
        source_labels = sorted(
            {str(row["source_side"]) for row in source_rows if row.get("source_side")}
        )
        source_originals = [
            row for row in source_rows if row.get("wikidisputes_type_exact") == "original"
        ]
        final_repr = next(
            (
                row
                for row in representation_by_logical[logical_uid]
                if row["representation_kind"] == "wikiconv_final_text_exact"
            ),
            None,
        )
        creation_action_row = next(
            (row for row in action_rows if row["action_type"] == "creation"), None
        )
        creation_repr = next(
            (
                row
                for row in representation_by_logical[logical_uid]
                if creation_action_row
                and row["version_uid"] == creation_action_row["version_uid"]
                and row["representation_kind"]
                in {"wikiconv_action_text_exact", "wikidisputes_text_exact"}
            ),
            None,
        )
        source_repr = next(
            (
                row
                for row in representation_by_logical[logical_uid]
                if row["representation_kind"] == "wikidisputes_text_exact"
            ),
            None,
        )
        exact_source_row = (
            source_originals[0] if source_originals else source_rows[0] if source_rows else None
        )
        selected_representation = final_repr or source_repr or creation_repr
        utterances.append(
            {
                "logical_utterance_uid": logical_uid,
                "conversation_uid": "wikiconv-conversation:" + str(creation["conversation_id"]),
                "conversation_id_exact": creation["conversation_id"],
                "identity_method": identity_method_by_logical[logical_uid],
                "identity_algorithm_version": IDENTITY_VERSION,
                "created_at_utc": creation["created_at"],
                "created_at_status": "wikiconv_creation_time" if wc_rows else "source_unvalidated",
                "creation_revision_id": _id_parts(creation["creation_id"])[0],
                "utterance_order": order_by_logical[logical_uid],
                "simultaneity_group_id": simultaneity_by_logical[logical_uid],
                "in_wikidisputes_release": bool(source_rows),
                "in_source_projection_as_creation": bool(source_originals),
                "in_full_rehydrated_thread": bool(wc_rows),
                "additional_rehydrated_absent_from_wikidisputes": bool(wc_rows and not source_rows),
                "in_episode_window": False,
                "predictor_eligible": False,
                "outcome_eligible": False,
                "episode_membership_count": len(episodes),
                "primary_episode_uid": episodes[0]["episode_uid"] if len(episodes) == 1 else None,
                "source_label_provenance_json": json.dumps(source_labels),
                "source_row_count": len(source_rows),
                "wikidisputes_text_exact": (
                    exact_source_row.get("wikidisputes_text_exact") if exact_source_row else None
                ),
                "wikidisputes_user_exact": (
                    exact_source_row.get("wikidisputes_user_exact") if exact_source_row else None
                ),
                "wikidisputes_user_values_json": json.dumps(
                    sorted(
                        {
                            str(row["wikidisputes_user_exact"])
                            for row in source_rows
                            if row.get("wikidisputes_user_exact") is not None
                        }
                    ),
                    ensure_ascii=False,
                ),
                "wikiconv_speaker_exact": (
                    representative_wc.get("wikiconv_speaker_exact") if representative_wc else None
                ),
                "canonical_selected_text_sha256": (
                    selected_representation.get("content_sha256")
                    if selected_representation
                    else None
                ),
                "action_count": len(action_rows),
                "modification_count": types.count("modification"),
                "deletion_count": types.count("deletion"),
                "restoration_count": types.count("restoration"),
                "was_modified": "modification" in types,
                "modified_after_first_reply": None,
                "post_cutoff_modification": None,
                "wikidisputes_text_representation_uid": (
                    source_repr["representation_uid"] if source_repr else None
                ),
                "final_text_representation_uid": final_repr["representation_uid"]
                if final_repr
                else None,
                "creation_text_representation_uid": (
                    creation_repr["representation_uid"]
                    if creation_repr
                    else source_repr["representation_uid"]
                    if source_repr
                    else None
                ),
                "pre_first_reply_representation_uid": None,
                "predictor_cutoff_representation_uid": None,
                "revision_wikitext_representation_uid": None,
                "rendered_html_reconstructed_representation_uid": None,
                "visible_text_reconstructed_representation_uid": None,
                "link_count": 0,
                "signature_count": 0,
                "link_child_key": f"logical_utterance_uid={logical_uid}",
                "signature_child_key": f"logical_utterance_uid={logical_uid}",
                "reply_target_logical_uid": None,
                "recovery_status": "recovered_from_wikiconv"
                if wc_rows
                else "source_only_unresolved",
                "recovery_method": "pinned_annual_corpus_union" if wc_rows else "source_projection",
                "available_at": creation["created_at"],
                "leakage_class": "creation_time_available",
                "quality_status": "unresolved" if not wc_rows else "recovered",
                "schema_version": SCHEMA_VERSION,
            }
        )

    selected_hash_to_logical: dict[str, list[str]] = defaultdict(list)
    for utterance in utterances:
        logical_uid = str(utterance["logical_utterance_uid"])
        selected_hash = utterance.get("canonical_selected_text_sha256")
        text = utterance.get("wikidisputes_text_exact")
        if not isinstance(text, str):
            final_representation = next(
                (
                    row
                    for row in representation_by_logical[logical_uid]
                    if row.get("representation_uid")
                    == utterance.get("final_text_representation_uid")
                ),
                None,
            )
            text = final_representation.get("content_inline") if final_representation else None
        if selected_hash and isinstance(text, str) and text:
            selected_hash_to_logical[str(selected_hash)].append(logical_uid)
        flag_specs: list[tuple[bool, str, str, str]] = [
            (
                not utterance.get("wikidisputes_user_exact")
                and not utterance.get("wikiconv_speaker_exact"),
                "missing_author_evidence",
                "warning",
                "neither source username nor WikiConv speaker is observed",
            ),
            (
                not isinstance(text, str) or not text,
                "empty_or_unavailable_text",
                "warning",
                "selected exact text is empty or unavailable",
            ),
            (
                isinstance(text, str) and len(text.split()) > 1000,
                "comment_over_1000_whitespace_tokens",
                "info",
                f"whitespace_tokens={len(text.split()) if isinstance(text, str) else 0}",
            ),
            (
                isinstance(text, str) and text.count("(UTC)") >= 2,
                "absorbed_multi_turn_candidate",
                "warning",
                "multiple signature timestamp markers; no automatic split",
            ),
        ]
        for applies, code, severity, evidence_pointer in flag_specs:
            if applies:
                quality.append(
                    {
                        "quality_flag_uid": _uid("wdquality", logical_uid, code),
                        "entity_uid": logical_uid,
                        "flag_code": code,
                        "severity": severity,
                        "evidence_pointer": evidence_pointer,
                    }
                )
    for content_hash, logical_uids in selected_hash_to_logical.items():
        if len(logical_uids) < 2:
            continue
        for logical_uid in logical_uids:
            quality.append(
                {
                    "quality_flag_uid": _uid(
                        "wdquality", logical_uid, "exact_text_duplicate_candidate", content_hash
                    ),
                    "entity_uid": logical_uid,
                    "flag_code": "exact_text_duplicate_candidate",
                    "severity": "info",
                    "evidence_pointer": json.dumps(
                        {"count": len(logical_uids), "sample_uids": sorted(logical_uids)[:20]}
                    ),
                }
            )
    for anchor, candidates in source_resolution_candidates.items():
        if len(candidates) <= 1:
            continue
        quality.append(
            {
                "quality_flag_uid": _uid("wdquality", anchor, "ambiguous_identity_candidates"),
                "entity_uid": source_anchor_to_logical[anchor],
                "flag_code": "ambiguous_identity_candidates",
                "severity": "error",
                "evidence_pointer": json.dumps(candidates),
            }
        )

    # Reply aliases are conversation-scoped. WikiConv identifiers are usually
    # globally unique, but scoping prevents a repeated source alias in a
    # contradictory/cross-label record from resolving to the wrong thread.
    alias_to_logical: dict[tuple[str, str], set[str]] = defaultdict(set)
    for logical_uid in all_logical_uids:
        conversation_id = str(creation_by_logical[logical_uid]["conversation_id"])
        for row in wc_by_logical.get(logical_uid, []):
            for value in (
                row.get("wikiconv_id_exact"),
                row.get("ancestor_id_exact"),
                row.get("parent_id_exact"),
            ):
                if value:
                    alias_to_logical[(conversation_id, str(value))].add(logical_uid)
            for lifecycle in _wikiconv_lifecycle(row):
                if lifecycle.get("id"):
                    alias_to_logical[(conversation_id, str(lifecycle["id"]))].add(logical_uid)
        for row in source_by_logical.get(logical_uid, []):
            for value in (
                row.get("wikidisputes_id_exact"),
                row.get("wikidisputes_original_id_exact"),
            ):
                if value:
                    alias_to_logical[(conversation_id, str(value))].add(logical_uid)
    replies: list[dict[str, Any]] = []
    for logical_uid in all_logical_uids:
        wc = wc_by_logical.get(logical_uid, [])
        src = source_by_logical.get(logical_uid, [])
        representative = wc[0] if wc else (src[0] if src else {})
        raw_target = representative.get("wikiconv_reply_to_exact") or representative.get(
            "wikidisputes_reply_to_exact"
        )
        conversation_id = str(creation_by_logical[logical_uid]["conversation_id"])
        candidates = (
            sorted(alias_to_logical.get((conversation_id, str(raw_target)), set()))
            if raw_target
            else []
        )
        target = candidates[0] if len(candidates) == 1 else None
        source_time = _parse_iso(creation_by_logical[logical_uid]["created_at"])
        target_time = _parse_iso(creation_by_logical[target]["created_at"]) if target else None
        self_reference = target == logical_uid
        replies.append(
            {
                "reply_edge_uid": _uid("wdreply", logical_uid, raw_target),
                "source_logical_utterance_uid": logical_uid,
                "source_row_uid": src[0]["source_row_uid"] if src else None,
                "raw_reply_target": raw_target,
                "repaired_reply_target": raw_target if target else None,
                "target_logical_utterance_uid": target,
                "target_utterance_order": order_by_logical.get(target) if target else None,
                "resolution_method": "unique_conversation_scoped_alias" if target else "none",
                "resolution_status": "resolved"
                if target
                else ("root_or_context" if raw_target is None else "unresolved"),
                "resolution_confidence": "high" if target else "none",
                "error_reason": None
                if target or raw_target is None
                else "no_unique_alias_or_context_target",
                "self_reference": self_reference,
                "child_before_parent": bool(
                    source_time and target_time and source_time < target_time
                ),
                "equal_time": bool(source_time and target_time and source_time == target_time),
                "structural_depth": None,
                "thread_root_logical_uid": None,
                "reply_lag_seconds": (
                    (source_time - target_time).total_seconds()
                    if source_time and target_time
                    else None
                ),
                "indentation": representative.get("indentation_exact"),
                "inferred_addressee": None,
                "schema_version": SCHEMA_VERSION,
            }
        )
        if self_reference:
            quality.append(
                {
                    "quality_flag_uid": _uid("wdquality", logical_uid, "reply_self_reference"),
                    "entity_uid": logical_uid,
                    "flag_code": "reply_self_reference",
                    "severity": "error",
                    "evidence_pointer": f"reply:{raw_target}",
                }
            )

    # Derive reply depth and root only for acyclic resolved parent chains. A
    # deterministic tie-break order never substitutes for structural evidence.
    reply_by_source = {str(row["source_logical_utterance_uid"]): row for row in replies}
    for start_uid, reply in reply_by_source.items():
        seen: set[str] = set()
        node = start_uid
        depth = 0
        cycle = False
        while True:
            if node in seen:
                cycle = True
                quality.append(
                    {
                        "quality_flag_uid": _uid("wdquality", start_uid, "reply_cycle"),
                        "entity_uid": start_uid,
                        "flag_code": "reply_cycle",
                        "severity": "error",
                        "evidence_pointer": json.dumps(sorted(seen)),
                    }
                )
                break
            seen.add(node)
            parent = reply_by_source.get(node, {}).get("target_logical_utterance_uid")
            if parent is None:
                break
            depth += 1
            node = str(parent)
        if not cycle:
            reply["structural_depth"] = depth
            reply["thread_root_logical_uid"] = node

    first_reply_by_target: dict[str, dt.datetime] = {}
    for reply in replies:
        target_uid = reply.get("target_logical_utterance_uid")
        source_uid = str(reply["source_logical_utterance_uid"])
        reply_time = _parse_iso(creation_by_logical[source_uid]["created_at"])
        if not target_uid or reply_time is None:
            continue
        current = first_reply_by_target.get(str(target_uid))
        if current is None or reply_time < current:
            first_reply_by_target[str(target_uid)] = reply_time
    link_counts = Counter(str(row["logical_utterance_uid"]) for row in links)
    signature_counts = Counter(str(row["logical_utterance_uid"]) for row in signatures)
    for utterance in utterances:
        logical_uid = str(utterance["logical_utterance_uid"])
        utterance_reply = reply_by_source.get(logical_uid)
        utterance["reply_target_logical_uid"] = (
            utterance_reply.get("target_logical_utterance_uid") if utterance_reply else None
        )
        utterance["link_count"] = link_counts[logical_uid]
        utterance["signature_count"] = signature_counts[logical_uid]
        modification_times = [
            parsed
            for action in action_by_logical[logical_uid]
            if action["action_type"] == "modification"
            and (parsed := _parse_iso(action.get("raw_timestamp"))) is not None
        ]
        first_reply = first_reply_by_target.get(logical_uid)
        utterance["modified_after_first_reply"] = (
            any(value > first_reply for value in modification_times)
            if first_reply is not None and modification_times
            else False
            if first_reply is not None
            else None
        )
        episodes_for_utterance = episode_by_conversation.get(
            str(utterance["conversation_id_exact"]), []
        )
        indexes = [
            parsed
            for episode in episodes_for_utterance
            if (parsed := _parse_iso(episode.get("episode_index_at"))) is not None
        ]
        utterance["post_cutoff_modification"] = (
            any(action_time > index for action_time in modification_times for index in indexes)
            if indexes and modification_times
            else False
            if indexes
            else None
        )

    contexts: list[dict[str, Any]] = []
    source_context_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        context_uid = source_to_context.get(str(row["source_row_uid"]))
        if context_uid:
            source_context_rows[context_uid].append(row)
    for context_uid, wc_rows in sorted(wc_context_by_uid.items()):
        ordered_wc = sorted(
            wc_rows,
            key=lambda row: (int(row["corpus_year"]), int(row["source_line_index"])),
        )
        representative = ordered_wc[0]
        lifecycle: list[dict[str, Any]] = []
        seen_context_actions: set[tuple[str, str, str]] = set()
        for wc_row in ordered_wc:
            for action in _wikiconv_lifecycle(wc_row):
                key = (
                    str(action.get("id")),
                    str(action.get("action_type")),
                    canonical_json_hash(action),
                )
                if key not in seen_context_actions:
                    seen_context_actions.add(key)
                    lifecycle.append(
                        {**action, "wikiconv_source_row_uid": wc_row["wikiconv_source_row_uid"]}
                    )
        creation = next(
            (row for row in lifecycle if row.get("action_type") == "creation"),
            lifecycle[0] if lifecycle else {},
        )
        conversation_id = str(representative["conversation_id_exact"])
        context_source_rows = source_context_rows.get(context_uid, [])
        contexts.append(
            {
                "context_node_uid": context_uid,
                "conversation_uid": "wikiconv-conversation:" + conversation_id,
                "source_row_uid": (
                    context_source_rows[0]["source_row_uid"] if context_source_rows else None
                ),
                "source_row_uids_json": json.dumps(
                    sorted(str(row["source_row_uid"]) for row in context_source_rows)
                ),
                "context_kind": "wikiconv_section_header_or_subject",
                "text_exact": representative.get("wikiconv_text_exact"),
                "created_at_utc": _iso_from_unix(creation.get("timestamp")),
                "display_order": None,
                "annotation_eligible": False,
                "recovery_status": "recovered_from_pinned_wikiconv_is_section_header",
                "schema_version": SCHEMA_VERSION,
            }
        )
        for action_index, action in enumerate(lifecycle):
            action_uid = _uid(
                "wdcontextaction",
                context_uid,
                action.get("action_type"),
                action.get("id"),
                action_index,
            )
            version_uid = _uid("wdcontextversion", action_uid)
            context_meta = action.get("meta_dict")
            context_meta = context_meta if isinstance(context_meta, dict) else {}
            context_actions.append(
                {
                    "context_action_uid": action_uid,
                    "context_version_uid": version_uid,
                    "context_node_uid": context_uid,
                    "action_type": action.get("action_type"),
                    "action_id_exact": action.get("id"),
                    "revision_id": _revision_id(context_meta.get("rev_id"), action.get("id")),
                    "raw_timestamp": _iso_from_unix(action.get("timestamp")),
                    "wikiconv_source_row_uid": action.get("wikiconv_source_row_uid"),
                    "raw_action_json_canonical": json.dumps(
                        action, ensure_ascii=False, sort_keys=True, default=str
                    ),
                    "recovery_status": "recovered_from_pinned_wikiconv",
                    "schema_version": SCHEMA_VERSION,
                }
            )
            context_text = action.get("text")
            context_encoded = (context_text or "").encode("utf-8")
            context_representations.append(
                {
                    "context_representation_uid": _uid(
                        "wdcontextrepr", version_uid, "wikiconv_context_text_exact"
                    ),
                    "context_node_uid": context_uid,
                    "context_version_uid": version_uid,
                    "representation_kind": "wikiconv_context_text_exact",
                    "content_sha256": sha256_bytes(context_encoded),
                    "byte_length": len(context_encoded),
                    "encoding": "utf-8",
                    "mime_type": "text/plain",
                    "content_inline": context_text,
                    "availability_status": "available" if context_text is not None else "unknown",
                    "available_at": _iso_from_unix(action.get("timestamp")),
                    "evidence_pointer": (
                        f"wikiconv_source_row:{action.get('wikiconv_source_row_uid')}"
                    ),
                    "representation_version": REPRESENTATION_VERSION,
                }
            )
        for alias_value, alias_kind in {
            (str(value), alias_kind)
            for row in ordered_wc
            for value, alias_kind in (
                (row.get("wikiconv_id_exact"), "wikiconv_current_id"),
                (row.get("ancestor_id_exact"), "wikiconv_ancestor_id"),
            )
            if value
        }:
            aliases.append(
                {
                    "alias_uid": _uid("wdalias", context_uid, alias_kind, alias_value),
                    "alias_namespace": alias_kind,
                    "alias_value_exact": alias_value,
                    "source_row_uid": None,
                    "wikiconv_source_row_uid": representative["wikiconv_source_row_uid"],
                    "entity_kind": "context_node",
                    "resolved_entity_uid": context_uid,
                    "resolution_status": "resolved",
                    "validity_status": "observed",
                    "evidence_pointer": f"context_node:{context_uid}",
                    "schema_version": SCHEMA_VERSION,
                }
            )
    existing_context_uids = {str(row["context_node_uid"]) for row in contexts}
    for context_uid, source_rows_for_context in source_context_rows.items():
        if context_uid in existing_context_uids:
            continue
        representative = min(source_rows_for_context, key=lambda row: row["source_order"])
        contexts.append(
            {
                "context_node_uid": context_uid,
                "conversation_uid": "wikiconv-conversation:"
                + str(representative["wikidisputes_conv_id_exact"]),
                "source_row_uid": representative["source_row_uid"],
                "source_row_uids_json": json.dumps(
                    sorted(str(row["source_row_uid"]) for row in source_rows_for_context)
                ),
                "context_kind": "source_only_section_header_candidate",
                "text_exact": representative.get("wikidisputes_text_exact"),
                "created_at_utc": representative.get("wikidisputes_time"),
                "display_order": None,
                "annotation_eligible": False,
                "recovery_status": "source_only_unresolved_context_candidate",
                "schema_version": SCHEMA_VERSION,
            }
        )
    for context_uid, source_rows_for_context in source_context_rows.items():
        for source_row in source_rows_for_context:
            for column in (
                "wikidisputes_id_exact",
                "wikidisputes_original_id_exact",
                "wikidisputes_reply_to_exact",
            ):
                value = source_row.get(column)
                if value is None:
                    continue
                aliases.append(
                    {
                        "alias_uid": _uid(
                            "wdalias", source_row["source_row_uid"], "wikidisputes", column, value
                        ),
                        "alias_namespace": f"wikidisputes_{column}",
                        "alias_value_exact": value,
                        "source_row_uid": source_row["source_row_uid"],
                        "wikiconv_source_row_uid": None,
                        "entity_kind": "context_node",
                        "resolved_entity_uid": context_uid,
                        "resolution_status": "resolved"
                        if column != "wikidisputes_reply_to_exact"
                        else "target_alias_observed",
                        "validity_status": "observed",
                        "evidence_pointer": f"source_row:{source_row['source_row_uid']}",
                        "schema_version": SCHEMA_VERSION,
                    }
                )
    # Add stable talk-page context from exact conversation metadata alongside,
    # but distinct from, WikiConv's explicitly flagged section/title nodes.
    if metadata_path.exists():
        known_context_uids = {str(row["context_node_uid"]) for row in contexts}
        for metadata in pq.read_table(metadata_path).to_pylist():
            conversation_id = str(metadata["conversation_id_exact"])
            conversation_uid = "wikiconv-conversation:" + conversation_id
            context_uid = _uid("wdcontext", conversation_uid, "talk_page_context")
            if context_uid in known_context_uids:
                continue
            raw_metadata = str(metadata["metadata_json_exact"])
            parsed_metadata = json.loads(raw_metadata)
            meta = parsed_metadata.get("meta", {})
            contexts.append(
                {
                    "context_node_uid": context_uid,
                    "conversation_uid": conversation_uid,
                    "source_row_uid": None,
                    "context_kind": "talk_page_context",
                    "text_exact": meta.get("page_title"),
                    "page_id_exact": meta.get("page_id"),
                    "metadata_json_exact": raw_metadata,
                    "metadata_sha256": metadata["metadata_sha256"],
                    "display_order": None,
                    "annotation_eligible": False,
                    "recovery_status": "recovered_from_pinned_wikiconv_metadata",
                    "schema_version": SCHEMA_VERSION,
                }
            )
            known_context_uids.add(context_uid)
    display: list[dict[str, Any]] = []
    context_by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for context in contexts:
        context_by_conversation[str(context["conversation_uid"])].append(context)
    utterance_by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for utterance in utterances:
        utterance_by_conversation[str(utterance["conversation_uid"])].append(utterance)
    for conversation_uid in sorted(set(context_by_conversation) | set(utterance_by_conversation)):
        position = 1
        for context in sorted(
            context_by_conversation[conversation_uid], key=lambda row: row["context_node_uid"]
        ):
            context["display_order"] = position
            display.append(
                {
                    "display_row_uid": _uid("wddisplay", context["context_node_uid"]),
                    "conversation_uid": conversation_uid,
                    "row_kind": "context",
                    "context_node_uid": context["context_node_uid"],
                    "logical_utterance_uid": None,
                    "display_order": position,
                    "utterance_order": None,
                    "annotation_eligible": False,
                    "text_exact": context.get("text_exact"),
                }
            )
            position += 1
        for utterance in sorted(
            utterance_by_conversation[conversation_uid], key=lambda row: row["utterance_order"]
        ):
            logical_uid = str(utterance["logical_utterance_uid"])
            text_repr = next(
                (
                    row
                    for row in representation_by_logical[logical_uid]
                    if row["representation_uid"]
                    == (
                        utterance["final_text_representation_uid"]
                        or utterance["wikidisputes_text_representation_uid"]
                    )
                ),
                None,
            )
            display.append(
                {
                    "display_row_uid": _uid("wddisplay", logical_uid),
                    "conversation_uid": conversation_uid,
                    "row_kind": "utterance",
                    "context_node_uid": None,
                    "logical_utterance_uid": logical_uid,
                    "display_order": position,
                    "utterance_order": utterance["utterance_order"],
                    "annotation_eligible": True,
                    "text_exact": text_repr.get("content_inline") if text_repr else None,
                }
            )
            position += 1

    episode_memberships: list[dict[str, Any]] = []
    for utterance in utterances:
        conversation_id = str(utterance["conversation_id_exact"])
        for episode in episode_by_conversation.get(conversation_id, []):
            episode_uid = str(episode["episode_uid"])
            episode_memberships.append(
                {
                    "episode_uid": episode_uid,
                    "logical_utterance_uid": utterance["logical_utterance_uid"],
                    "membership_uid": _uid(
                        "wdepisode-utterance", episode_uid, utterance["logical_utterance_uid"]
                    ),
                    "source_wikidisputes_escalated": episode["source_wikidisputes_escalated"],
                    "episode_index_at": episode.get("episode_index_at"),
                    "cutoff_rule_version": episode.get("cutoff_rule_version"),
                    "in_episode_window": False,
                    "predictor_eligible": False,
                    "outcome_eligible": False,
                    "analysis_status": episode.get("analysis_status"),
                    "dv_values_json": json.dumps(
                        outcomes_by_episode.get(episode_uid, []),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    "schema_version": SCHEMA_VERSION,
                }
            )

    # Refresh the future-annotation join contract with the authoritative full
    # identity resolution. Exact source fields and source hashes remain copied
    # from the immutable projection; no annotation or Gold data is consulted.
    old_join = pq.read_table(
        output_root / "silver" / "annotation_join_contract.parquet"
    ).to_pylist()
    utterance_by_uid = {str(row["logical_utterance_uid"]): row for row in utterances}
    context_uid_by_source = {
        str(row["source_row_uid"]): str(row["context_node_uid"])
        for row in contexts
        if row.get("source_row_uid")
    }
    context_uid_by_source.update(source_to_context)
    action_by_source = source_action_resolution
    display_order_by_uid = {
        str(row.get("logical_utterance_uid") or row.get("context_node_uid")): row["display_order"]
        for row in display
    }
    reply_target_by_uid = {
        str(row["source_logical_utterance_uid"]): row["target_logical_utterance_uid"]
        for row in replies
    }
    refreshed_join: list[dict[str, Any]] = []
    for row in old_join:
        source_uid = str(row["source_row_uid"])
        resolved_logical_uid = source_to_logical.get(source_uid)
        resolved_context_uid = context_uid_by_source.get(source_uid)
        source_action = action_by_source.get(source_uid)
        resolved_utterance = (
            utterance_by_uid.get(resolved_logical_uid) if resolved_logical_uid else None
        )
        row.update(
            {
                "logical_utterance_uid": resolved_logical_uid,
                "context_node_uid": resolved_context_uid,
                "version_uid": source_action["version_uid"] if source_action else None,
                "action_uid": source_action["action_uid"] if source_action else None,
                "utterance_order": (
                    resolved_utterance["utterance_order"] if resolved_utterance else None
                ),
                "display_order": display_order_by_uid.get(
                    resolved_logical_uid or resolved_context_uid or ""
                ),
                "reply_target_logical_uid": reply_target_by_uid.get(resolved_logical_uid or ""),
                "identity_algorithm_version": IDENTITY_VERSION,
                "join_contract_version": JOIN_CONTRACT_VERSION,
            }
        )
        refreshed_join.append(row)

    context_join_contract = [
        {
            "context_node_uid": context["context_node_uid"],
            "conversation_uid": context["conversation_uid"],
            "source_row_uid": context.get("source_row_uid"),
            "context_kind": context.get("context_kind"),
            "text_exact": context.get("text_exact"),
            "display_order": display_order_by_uid.get(str(context["context_node_uid"])),
            "annotation_eligible": False,
            "schema_version": SCHEMA_VERSION,
            "identity_algorithm_version": IDENTITY_VERSION,
            "join_contract_version": JOIN_CONTRACT_VERSION,
        }
        for context in contexts
    ]

    old_registry_by_anchor = {
        str(row["selected_anchor"]): row
        for row in existing_registry
        if row.get("selected_anchor") is not None
    }
    for row in registry:
        prior = old_registry_by_anchor.get(str(row.get("selected_anchor")))
        if prior and prior.get("issued_uid") != row.get("issued_uid"):
            aliases.append(
                {
                    "alias_uid": _uid(
                        "wdalias",
                        "identity_redirect",
                        prior["issued_uid"],
                        row["issued_uid"],
                        SCHEMA_VERSION,
                    ),
                    "alias_namespace": "identity_registry_redirect",
                    "alias_value_exact": prior["issued_uid"],
                    "source_row_uid": None,
                    "wikiconv_source_row_uid": None,
                    "entity_kind": "identity_redirect",
                    "resolved_entity_uid": row["issued_uid"],
                    "resolution_status": "redirect_effective",
                    "validity_status": "append_only_historical_alias",
                    "effective_version": SCHEMA_VERSION,
                    "evidence_pointer": f"registry_entry:{row['registry_entry_uid']}",
                    "schema_version": SCHEMA_VERSION,
                }
            )
    registry = _append_only_registry(existing_registry, registry)
    artifacts: dict[str, Any] = {}
    silver = output_root / "silver"
    canonical = output_root / "canonical"
    for name, artifact_rows in (
        ("utterances", utterances),
        ("utterance_actions", actions),
        ("utterance_versions", actions),
        ("context_actions", context_actions),
        ("context_representations", context_representations),
        ("utterance_representations", representations),
        ("source_id_aliases", aliases),
        ("identity_registry", registry),
        ("reply_edges", replies),
        ("authors_actors", actor_rows),
        ("signatures", signatures),
        ("links", links),
        ("quality_flags", quality),
        ("episode_utterances", episode_memberships),
        ("dispute_episodes", episode_rows),
        ("context_nodes", contexts),
        ("annotation_join_contract", refreshed_join),
        ("annotation_context_join_contract", context_join_contract),
    ):
        artifacts[name] = _write(silver / f"{name}.parquet", artifact_rows)
    for export_name, silver_name, row_count in (
        ("wikidisputes_utterances_ssot", "utterances", len(utterances)),
        (
            "wikidisputes_episode_utterances_ssot",
            "episode_utterances",
            len(episode_memberships),
        ),
    ):
        target = canonical / f"{export_name}.parquet"
        atomic_link_or_copy(silver / f"{silver_name}.parquet", target)
        artifacts[export_name] = {**file_descriptor(target), "rows": row_count}
    artifacts["wikidisputes_annotation_display"] = _write(
        canonical / "wikidisputes_annotation_display.parquet", display
    )

    report = {
        "status": enumeration_report["status"],
        "join_contract_version": JOIN_CONTRACT_VERSION,
        "conversational_completeness_claim": enumeration_report["status"] == "complete",
        "counts": {
            "source_rows": len(source),
            "wikiconv_rows_before_identical_observation_dedup": len(wikiconv_all),
            "wikiconv_observations": len(wikiconv),
            "logical_utterances": len(utterances),
            "source_logical_utterances": sum(
                bool(row["in_wikidisputes_release"]) for row in utterances
            ),
            "additional_rehydrated_utterances": sum(
                bool(row["additional_rehydrated_absent_from_wikidisputes"]) for row in utterances
            ),
            "context_nodes": len(contexts),
            "context_actions": len(context_actions),
            "context_representations": len(context_representations),
            "actions": len(actions),
            "modifications": sum(row["action_type"] == "modification" for row in actions),
            "deletions": sum(row["action_type"] == "deletion" for row in actions),
            "restorations": sum(row["action_type"] == "restoration" for row in actions),
            "source_only_unresolved_logical": sum(
                row["recovery_status"] == "source_only_unresolved" for row in utterances
            ),
            "unavailable_or_suppressed_actions": sum(
                row.get("recovery_status") in {"unavailable", "hidden", "suppressed"}
                for row in actions
            ),
            "episode_memberships": len(episode_memberships),
            "signatures": len(signatures),
            "links": len(links),
        },
        "enumeration": enumeration_report,
        "artifacts": artifacts,
    }
    report["cross_label_reconciliation"] = materialize_cross_label_reconciliation(output_root)
    atomic_write_json(output_root / "reports" / "full_rehydration.json", report)
    return report
