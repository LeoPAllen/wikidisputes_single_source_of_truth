from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .constants import (
    CROSS_LABEL_DISCUSSION_IDS,
    IDENTITY_VERSION,
    JOIN_CONTRACT_VERSION,
    REPRESENTATION_VERSION,
    SCHEMA_VERSION,
)
from .hashing import canonical_json_hash, sha256_bytes
from .io import atomic_parquet, atomic_write_json, file_descriptor


def _uid(namespace: str, *parts: Any) -> str:
    return f"{namespace}:v1:" + canonical_json_hash(list(parts))


def _creation_anchor(row: dict[str, Any]) -> tuple[str, str]:
    original = row.get("wikidisputes_original_id_exact")
    current = row.get("wikidisputes_id_exact")
    action_type = row.get("wikidisputes_type_exact")
    if isinstance(original, str) and original:
        return original, "wikiconv_original_id"
    if action_type == "original" and isinstance(current, str) and current:
        return current, "wikiconv_creation_id"
    if isinstance(current, str) and current:
        # No text/order data participates: this is an immutable source-alias fallback.
        return current, "source_action_alias_fallback"
    return str(row["source_row_uid"]), "source_row_location_fallback"


def _logical_uid(row: dict[str, Any]) -> tuple[str, str, str]:
    anchor, method = _creation_anchor(row)
    if method in {"wikiconv_original_id", "wikiconv_creation_id"}:
        return f"wikiconv:{anchor}", method, anchor
    return "wdutt:fallback:v1:" + canonical_json_hash(["immutable-alias", anchor]), method, anchor


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _id_components(value: str | None) -> tuple[int, int, int]:
    if not value:
        return (2**63 - 1, 2**63 - 1, 2**63 - 1)
    parts = value.split(".")
    parsed: list[int] = []
    for part in parts[:3]:
        try:
            parsed.append(int(part))
        except ValueError:
            parsed.append(2**63 - 1)
    return tuple((parsed + [2**63 - 1] * 3)[:3])  # type: ignore[return-value]


def _write(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    table = pa.Table.from_pylist(rows) if rows else pa.table({"_empty": pa.array([], pa.string())})
    atomic_parquet(path, table)
    return {**file_descriptor(path), "rows": len(rows)}


def materialize_source_core(projection_path: Path, output_root: Path) -> dict[str, Any]:
    """Materialize source-evidence core without pretending WikiConv hydration is complete."""
    source = pq.read_table(projection_path).to_pylist()
    silver = output_root / "silver"
    canonical = output_root / "canonical"
    manifests = output_root / "manifests"
    for directory in (silver, canonical, manifests):
        directory.mkdir(parents=True, exist_ok=True)

    selected: dict[str, dict[str, Any]] = {}
    memberships: list[dict[str, Any]] = []
    disputes: dict[str, dict[str, Any]] = {}
    episodes: dict[str, dict[str, Any]] = {}
    episode_threads: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    registry_path = silver / "identity_registry.parquet"
    registry: dict[str, dict[str, Any]] = (
        {str(row["registry_entry_uid"]): row for row in pq.read_table(registry_path).to_pylist()}
        if registry_path.exists()
        else {}
    )
    actions: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    actor_rows: list[dict[str, Any]] = []
    utterance_sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    context_sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    case_first_rows: dict[str, int] = {}

    for row in source:
        case_uid = str(row["source_case_uid"])
        case_first_rows[case_uid] = min(
            case_first_rows.get(case_uid, 2**63 - 1), row["source_row_index"]
        )

    for row in source:
        conv_id = row.get("wikidisputes_conv_id_exact") or row.get("wikidisputes_id_exact")
        if not conv_id:
            conv_id = "unresolved:" + str(row["source_case_uid"])
        conversation_uid = "wikiconv-conversation:" + str(conv_id)
        selected.setdefault(
            conversation_uid,
            {
                "conversation_uid": conversation_uid,
                "conversation_id_exact": conv_id,
                "recovery_status": "pending_wikiconv_enumeration",
                "recovery_method": "selected_from_wikidisputes_conv_id",
                "in_wikidisputes_release": True,
                "years_scanned": [],
                "schema_version": SCHEMA_VERSION,
            },
        )
        memberships.append(
            {"conversation_uid": conversation_uid, "source_row_uid": row["source_row_uid"]}
        )
        dispute_uid = _uid("wddispute", row["source_case_uid"])
        disputes.setdefault(
            dispute_uid,
            {
                "dispute_uid": dispute_uid,
                "source_case_uid": row["source_case_uid"],
                "source_dispute_id_exact": row["source_dispute_id_exact"],
                "source_label_exact": row["source_side"],
                "source_wikidisputes_escalated": row["source_wikidisputes_escalated"],
                "dispute_json_canonical": row["source_dispute_json_canonical"],
                "schema_version": SCHEMA_VERSION,
            },
        )
        episode_uid = _uid("wdepisode", dispute_uid, "source-record-episode-v1")
        cross_label = conv_id in CROSS_LABEL_DISCUSSION_IDS
        dispute_metadata = json.loads(row["source_dispute_json_canonical"])
        pages = dispute_metadata.get("pages")
        source_title = (
            pages[0]
            if isinstance(pages, list) and len(pages) == 1 and isinstance(pages[0], str)
            else dispute_metadata.get("pagetitle")
        )
        episodes.setdefault(
            episode_uid,
            {
                "episode_uid": episode_uid,
                "dispute_uid": dispute_uid,
                "source_conversation_id_exact": conv_id,
                "thread_start_at": None,
                "source_projection_end_at": None,
                "episode_index_at": None,
                "dv_observation_start_at": None,
                "dv_observation_end_at": None,
                "observed_through_at": None,
                "cutoff_rule_version": "pending-evidence-v1",
                "alignment_status": "unresolved",
                "alignment_reasons": (
                    "source-only; page/section/participant/time/link/mediation components pending"
                ),
                "page_id_exact": None,
                "title_at_event_exact": source_title,
                "source_section_scope_exact": dispute_metadata.get("sec_name"),
                "page_identity_match_status": "pending_wikiconv_metadata",
                "section_scope_match_status": "not_observed",
                "participant_overlap_status": "not_computed",
                "participant_overlap_count": None,
                "temporal_distance_seconds": None,
                "temporal_alignment_status": "not_computed",
                "explicit_link_status": "source_url_observed"
                if dispute_metadata.get("url")
                else "not_observed",
                "mediation_summary_status": "source_outcome_observed"
                if dispute_metadata.get("outcome")
                else "not_applicable_or_not_observed",
                "page_move_history_status": "not_hydrated",
                "analysis_status": "quarantined_cross_label"
                if cross_label
                else "not_analysis_ready",
                "source_wikidisputes_escalated": row["source_wikidisputes_escalated"],
                "censoring_reason": "external_history_not_yet_enumerated",
                "schema_version": SCHEMA_VERSION,
            },
        )

        is_context_candidate = (
            row["source_row_index"] == case_first_rows[row["source_case_uid"]]
            and row.get("wikidisputes_id_exact") == conv_id
            and row.get("wikidisputes_reply_to_exact") is None
        )
        if is_context_candidate:
            context_uid = _uid("wdcontext", conversation_uid, row["source_row_uid"])
            context_sources[context_uid].append({**row, "conversation_uid": conversation_uid})
            represented_uid = context_uid
            entity_kind = "context_node"
            logical_uid = None
        else:
            logical_uid, method, anchor = _logical_uid(row)
            utterance_sources[logical_uid].append(
                {
                    **row,
                    "conversation_uid": conversation_uid,
                    "identity_method": method,
                    "identity_anchor": anchor,
                }
            )
            represented_uid = logical_uid
            entity_kind = "logical_utterance"
            registry_uid = _uid("wdregistry", logical_uid, IDENTITY_VERSION)
            registry.setdefault(
                registry_uid,
                {
                    "registry_entry_uid": registry_uid,
                    "issued_uid": logical_uid,
                    "entity_kind": "logical_utterance",
                    "derivation_method": method,
                    "selected_anchor": anchor,
                    "candidate_anchors_json": json.dumps([anchor]),
                    "confidence": "high" if method.startswith("wikiconv_") else "low",
                    "adjudication_status": "not_required"
                    if method.startswith("wikiconv_")
                    else "pending",
                    "algorithm_version": IDENTITY_VERSION,
                    "effective_version": SCHEMA_VERSION,
                    "registry_status": "active",
                },
            )

        for namespace, column in (
            ("wikidisputes_current_id", "wikidisputes_id_exact"),
            ("wikidisputes_original_id", "wikidisputes_original_id_exact"),
            ("wikiconv_conversation_id", "wikidisputes_conv_id_exact"),
            ("wikidisputes_parent_reply_to", "wikidisputes_reply_to_exact"),
        ):
            value = row.get(column)
            if value is None:
                continue
            aliases.append(
                {
                    "alias_uid": _uid("wdalias", row["source_row_uid"], namespace, value),
                    "alias_namespace": namespace,
                    "alias_value_exact": value,
                    "source_row_uid": row["source_row_uid"],
                    "entity_kind": entity_kind,
                    "resolved_entity_uid": represented_uid,
                    "resolution_status": "source_asserted"
                    if namespace != "wikidisputes_parent_reply_to"
                    else "unresolved_target",
                    "validity_status": "observed",
                    "evidence_pointer": f"source_row:{row['source_row_uid']}",
                    "schema_version": SCHEMA_VERSION,
                }
            )
        if logical_uid is not None:
            action_type = {
                "original": "creation",
                "modification": "modification",
                "restoration": "restoration",
                "deletion": "deletion",
            }.get(row.get("wikidisputes_type_exact"), "unknown")
            action_uid = _uid("wdaction", row["source_row_uid"])
            version_uid = _uid("wdversion", action_uid)
            actions.append(
                {
                    "action_uid": action_uid,
                    "version_uid": version_uid,
                    "logical_utterance_uid": logical_uid,
                    "source_row_uid": row["source_row_uid"],
                    "action_type": action_type,
                    "wikidisputes_current_id_exact": row["wikidisputes_id_exact"],
                    "wikidisputes_original_id_exact": row["wikidisputes_original_id_exact"],
                    "raw_timestamp": row["wikidisputes_time"],
                    "revision_id": _id_components(row["wikidisputes_id_exact"])[0],
                    "parent_action_id_exact": None,
                    "recovery_status": "source_projection_only",
                    "recovery_method": "wikidisputes_projection",
                    "schema_version": SCHEMA_VERSION,
                }
            )
            exact_text = row.get("wikidisputes_text_exact")
            content = (exact_text or "").encode("utf-8")
            representation_uid = _uid("wdrepr", row["source_row_uid"], "wikidisputes_text_exact")
            representations.append(
                {
                    "representation_uid": representation_uid,
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": row["source_row_uid"],
                    "representation_kind": "wikidisputes_text_exact",
                    "content_sha256": sha256_bytes(content),
                    "byte_length": len(content),
                    "encoding": "utf-8",
                    "mime_type": "text/plain",
                    "content_inline": exact_text,
                    "blob_path": None,
                    "source_revision_id": None,
                    "extraction_method": "json_decode_without_normalization",
                    "extraction_version": "1.0.0",
                    "availability_status": "available" if exact_text is not None else "unknown",
                    "leakage_class": "source_available",
                    "available_at": row["wikidisputes_time"],
                    "confidence": "exact_source_evidence",
                    "representation_version": REPRESENTATION_VERSION,
                }
            )
            actor_rows.append(
                {
                    "author_actor_uid": _uid("wdauthor", row["source_row_uid"]),
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": row["source_row_uid"],
                    "wikidisputes_user_exact": row["wikidisputes_user_exact"],
                    "wikiconv_speaker_exact": None,
                    "revision_actor_name_exact": None,
                    "revision_actor_user_id": None,
                    "identity_status": "source_username_only",
                    "resolved_identity": None,
                    "resolution_method": None,
                    "confidence": "unresolved",
                }
            )

    actions_by_logical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        actions_by_logical[action["logical_utterance_uid"]].append(action)

    seen_episode_threads: set[tuple[str, str]] = set()
    for episode in episodes.values():
        thread_uid = "wikiconv-conversation:" + str(episode["source_conversation_id_exact"])
        key = (episode["episode_uid"], thread_uid)
        if key not in seen_episode_threads:
            episode_threads.append(
                {
                    "episode_uid": episode["episode_uid"],
                    "thread_uid": thread_uid,
                    "conversation_uid": thread_uid,
                    "membership_status": "source_selected_pending_alignment",
                    "alignment_status": "unresolved",
                    "schema_version": SCHEMA_VERSION,
                }
            )
            seen_episode_threads.add(key)

    utterances: list[dict[str, Any]] = []
    utterance_order_map: dict[tuple[str, str], int] = {}
    simultaneity_map: dict[tuple[str, str, str | None], str] = {}
    by_conversation: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for uid, rows in utterance_sources.items():
        originals = [row for row in rows if row["wikidisputes_type_exact"] == "original"]
        anchor_row = min(originals or rows, key=lambda row: row["source_order"])
        by_conversation[anchor_row["conversation_uid"]].append((uid, anchor_row))
    for conversation_uid, items in by_conversation.items():
        items.sort(
            key=lambda item: (
                _parse_time(item[1]["wikidisputes_time"]) or datetime.max,
                *_id_components(item[1]["wikidisputes_id_exact"]),
                item[1]["source_row_index"],
                item[0],
            )
        )
        for position, (uid, anchor_row) in enumerate(items, start=1):
            utterance_order_map[(conversation_uid, uid)] = position
            key = (conversation_uid, "time", anchor_row["wikidisputes_time"])
            simultaneity_map.setdefault(key, _uid("wdsim", *key))

    for uid, rows in utterance_sources.items():
        originals = [row for row in rows if row["wikidisputes_type_exact"] == "original"]
        anchor_row = min(originals or rows, key=lambda row: row["source_order"])
        # Indexed below once, avoiding a full action-table scan per utterance.
        action_rows = actions_by_logical[uid]
        types = [action["action_type"] for action in action_rows]
        utterances.append(
            {
                "logical_utterance_uid": uid,
                "conversation_uid": anchor_row["conversation_uid"],
                "identity_method": anchor_row["identity_method"],
                "identity_anchor": anchor_row["identity_anchor"],
                "identity_algorithm_version": IDENTITY_VERSION,
                "created_at_utc": anchor_row["wikidisputes_time"] if originals else None,
                "created_at_status": "source_timestamp_unvalidated" if originals else "unresolved",
                "creation_revision_id": _id_components(anchor_row["wikidisputes_id_exact"])[0]
                if originals
                else None,
                "utterance_order": utterance_order_map[(anchor_row["conversation_uid"], uid)],
                "simultaneity_group_id": simultaneity_map[
                    (anchor_row["conversation_uid"], "time", anchor_row["wikidisputes_time"])
                ],
                "source_row_count": len(rows),
                "action_count": len(action_rows),
                "modification_count": types.count("modification"),
                "deletion_count": types.count("deletion"),
                "restoration_count": types.count("restoration"),
                "was_modified": "modification" in types,
                "in_wikidisputes_release": True,
                "in_full_rehydrated_thread": False,
                "recovery_status": "source_projection_only",
                "schema_version": SCHEMA_VERSION,
            }
        )

    contexts: list[dict[str, Any]] = []
    for context_uid, rows in context_sources.items():
        row = min(rows, key=lambda value: value["source_order"])
        contexts.append(
            {
                "context_node_uid": context_uid,
                "conversation_uid": row["conversation_uid"],
                "source_row_uid": row["source_row_uid"],
                "context_kind": "section_heading_candidate",
                "text_exact": row["wikidisputes_text_exact"],
                "display_order": 1,
                "annotation_eligible": False,
                "recovery_status": "candidate_pending_wikiconv_meta",
                "schema_version": SCHEMA_VERSION,
            }
        )

    reply_edges: list[dict[str, Any]] = []
    alias_to_uids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for uid, rows in utterance_sources.items():
        for row in rows:
            for value in (row["wikidisputes_id_exact"], row["wikidisputes_original_id_exact"]):
                if value:
                    alias_to_uids[(row["source_case_uid"], value)].add(uid)
    for uid, rows in utterance_sources.items():
        anchor_row = min(rows, key=lambda row: row["source_order"])
        raw_target = anchor_row["wikidisputes_reply_to_exact"]
        candidates = (
            sorted(alias_to_uids.get((anchor_row["source_case_uid"], raw_target), set()))
            if raw_target
            else []
        )
        target_uid = candidates[0] if len(candidates) == 1 else None
        source_time = _parse_time(anchor_row["wikidisputes_time"])
        target_time = None
        if target_uid:
            target_anchor = min(utterance_sources[target_uid], key=lambda row: row["source_order"])
            target_time = _parse_time(target_anchor["wikidisputes_time"])
        reply_edges.append(
            {
                "reply_edge_uid": _uid("wdreply", uid, anchor_row["source_row_uid"]),
                "source_logical_utterance_uid": uid,
                "source_row_uid": anchor_row["source_row_uid"],
                "raw_reply_target": raw_target,
                "repaired_reply_target": raw_target if target_uid else None,
                "target_logical_utterance_uid": target_uid,
                "target_utterance_order": utterance_order_map.get(
                    (anchor_row["conversation_uid"], target_uid)
                )
                if target_uid
                else None,
                "resolution_method": "same_case_current_or_original_alias"
                if target_uid
                else "none",
                "resolution_status": "resolved"
                if target_uid
                else ("root" if raw_target is None else "unresolved"),
                "resolution_confidence": "high" if target_uid else "none",
                "error_reason": None
                if target_uid or raw_target is None
                else "no_unique_same_case_alias",
                "self_reference": target_uid == uid,
                "child_before_parent": bool(
                    source_time and target_time and source_time < target_time
                ),
                "equal_time": bool(source_time and target_time and source_time == target_time),
                "structural_depth": None,
                "thread_root_logical_uid": None,
                "reply_lag_seconds": (source_time - target_time).total_seconds()
                if source_time and target_time
                else None,
                "indentation": None,
                "inferred_addressee": None,
                "schema_version": SCHEMA_VERSION,
            }
        )

    display: list[dict[str, Any]] = []
    contexts_by_conv = defaultdict(list)
    for context in contexts:
        contexts_by_conv[context["conversation_uid"]].append(context)
    utterances_by_conv = defaultdict(list)
    for utterance in utterances:
        utterances_by_conv[utterance["conversation_uid"]].append(utterance)
    for conversation_uid in sorted(set(contexts_by_conv) | set(utterances_by_conv)):
        position = 1
        for context in sorted(
            contexts_by_conv[conversation_uid], key=lambda row: row["context_node_uid"]
        ):
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
                    "text_exact": context["text_exact"],
                }
            )
            position += 1
        for utterance in sorted(
            utterances_by_conv[conversation_uid], key=lambda row: row["utterance_order"]
        ):
            rows = utterance_sources[utterance["logical_utterance_uid"]]
            anchor = min(rows, key=lambda row: row["source_order"])
            display.append(
                {
                    "display_row_uid": _uid("wddisplay", utterance["logical_utterance_uid"]),
                    "conversation_uid": conversation_uid,
                    "row_kind": "utterance",
                    "context_node_uid": None,
                    "logical_utterance_uid": utterance["logical_utterance_uid"],
                    "display_order": position,
                    "utterance_order": utterance["utterance_order"],
                    "annotation_eligible": True,
                    "text_exact": anchor["wikidisputes_text_exact"],
                }
            )
            position += 1

    display_order_by_entity: dict[str, int] = {}
    for item in display:
        entity_uid = item.get("logical_utterance_uid") or item.get("context_node_uid")
        if entity_uid:
            display_order_by_entity[str(entity_uid)] = int(item["display_order"])

    join_rows: list[dict[str, Any]] = []
    represented_by_source: dict[str, tuple[str | None, str | None]] = {}
    for uid, rows in utterance_sources.items():
        for row in rows:
            represented_by_source[row["source_row_uid"]] = (uid, None)
    for uid, rows in context_sources.items():
        for row in rows:
            represented_by_source[row["source_row_uid"]] = (None, uid)
    action_by_source = {action["source_row_uid"]: action for action in actions}
    reply_target_by_logical = {
        edge["source_logical_utterance_uid"]: edge["target_logical_utterance_uid"]
        for edge in reply_edges
    }
    episode_by_dispute = {episode["dispute_uid"]: episode for episode in episodes.values()}
    dispute_by_case = {dispute["source_case_uid"]: dispute for dispute in disputes.values()}
    for row in source:
        logical_uid, context_uid = represented_by_source[row["source_row_uid"]]
        action = action_by_source.get(row["source_row_uid"])
        dispute = dispute_by_case[row["source_case_uid"]]
        episode = episode_by_dispute[dispute["dispute_uid"]]
        conv = "wikiconv-conversation:" + str(
            row.get("wikidisputes_conv_id_exact")
            or row.get("wikidisputes_id_exact")
            or "unresolved:" + row["source_case_uid"]
        )
        selected_text_hash = sha256_bytes(
            (row.get("wikidisputes_text_exact") or "").encode("utf-8")
        )
        join_rows.append(
            {
                "join_row_uid": _uid("wdjoin", row["source_row_uid"], JOIN_CONTRACT_VERSION),
                "source_row_uid": row["source_row_uid"],
                "logical_utterance_uid": logical_uid,
                "context_node_uid": context_uid,
                "version_uid": action["version_uid"] if action else None,
                "action_uid": action["action_uid"] if action else None,
                "dispute_uid": dispute["dispute_uid"],
                "episode_uid": episode["episode_uid"],
                "conversation_uid": conv,
                "thread_uid": conv,
                "wikidisputes_current_id_exact": row["wikidisputes_id_exact"],
                "wikidisputes_original_id_exact": row["wikidisputes_original_id_exact"],
                "wikidisputes_parent_id_exact": None,
                "wikiconv_ancestor_id_exact": None,
                "aliases_json": json.dumps(
                    [
                        value
                        for value in (
                            row["wikidisputes_id_exact"],
                            row["wikidisputes_original_id_exact"],
                            row["wikidisputes_conv_id_exact"],
                            row["wikidisputes_reply_to_exact"],
                        )
                        if value
                    ],
                    ensure_ascii=False,
                ),
                "wikidisputes_text_exact": row["wikidisputes_text_exact"],
                "wikidisputes_user_exact": row["wikidisputes_user_exact"],
                "canonical_selected_text_sha256": selected_text_hash,
                "source_projection_sha256": row["source_projection_sha256"],
                "source_file": row["archive_member_path"],
                "source_case_index": row["source_case_index"],
                "source_row_index": row["source_row_index"],
                "source_order": row["source_order"],
                "utterance_order": utterance_order_map.get((conv, logical_uid))
                if logical_uid
                else None,
                "display_order": display_order_by_entity.get(logical_uid or context_uid),
                "reply_target_logical_uid": reply_target_by_logical.get(logical_uid)
                if logical_uid
                else None,
                "evidence_pointer": (
                    f"source_file:{row['source_file_uid']}"
                    f"#bytes={row['source_record_offset']}-"
                    f"{row['source_record_offset'] + row['source_record_length']}"
                ),
                "schema_version": SCHEMA_VERSION,
                "identity_algorithm_version": IDENTITY_VERSION,
                "join_contract_version": JOIN_CONTRACT_VERSION,
            }
        )

    artifacts: dict[str, Any] = {}
    for name, rows in (
        ("selected_conversations", list(selected.values())),
        ("conversation_source_membership", memberships),
        ("disputes", list(disputes.values())),
        ("dispute_episodes", list(episodes.values())),
        ("episode_threads", episode_threads),
        ("context_nodes", contexts),
        ("utterances", utterances),
        ("utterance_actions", actions),
        ("utterance_versions", actions),
        ("utterance_representations", representations),
        ("source_id_aliases", aliases),
        ("identity_registry", list(registry.values())),
        ("reply_edges", reply_edges),
        ("authors_actors", actor_rows),
        ("annotation_join_contract", join_rows),
    ):
        artifacts[name] = _write(silver / f"{name}.parquet", rows)
    artifacts["wikidisputes_annotation_display"] = _write(
        canonical / "wikidisputes_annotation_display.parquet", display
    )
    manifest = {
        "status": "source_core_materialized_not_conversation_complete",
        "artifacts": artifacts,
        "counts": {
            "source_rows": len(source),
            "selected_conversations": len(selected),
            "source_logical_utterances": len(utterances),
            "context_candidates": len(contexts),
            "actions": len(actions),
            "modifications": sum(row["action_type"] == "modification" for row in actions),
            "deletions": sum(row["action_type"] == "deletion" for row in actions),
            "restorations": sum(row["action_type"] == "restoration" for row in actions),
        },
    }
    atomic_write_json(manifests / "source_core.json", manifest)
    return manifest
