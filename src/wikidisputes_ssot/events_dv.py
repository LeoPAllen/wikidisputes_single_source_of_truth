from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .constants import DV_VERSION, SCHEMA_VERSION
from .hashing import canonical_json_hash
from .io import atomic_parquet, atomic_write_json, file_descriptor, table_from_union_pylist
from .reverts import IdentityRevert, detect_identity_reverts

DV_DEFINITIONS = [
    {
        "definition_id": "formal_escalation_drn",
        "definition_version": DV_VERSION,
        "semantic_definition": (
            "Observed DRN filing after episode index within horizon; source label is "
            "separate evidence."
        ),
        "definition_status": "candidate",
        "human_validation_gate": "stratified venue/alignment/time/evidence review",
    },
    {
        "definition_id": "durable_dispute_tag_clearance",
        "definition_version": DV_VERSION,
        "semantic_definition": (
            "Relevant tag removed after index and normalized family absent through horizon."
        ),
        "definition_status": "candidate",
        "human_validation_gate": "tag family, scope, removal and recurrence adjudication",
    },
    {
        "definition_id": "post_discussion_revert_stability",
        "definition_version": DV_VERSION,
        "semantic_definition": (
            "Identity/SHA-1 reverts in article history after index; low activity is not consensus."
        ),
        "definition_status": "candidate",
        "human_validation_gate": (
            "positive/negative/ambiguous SHA-1 revert fixtures and scope review"
        ),
    },
    {
        "definition_id": "formal_process_closure_outcome",
        "definition_version": DV_VERSION,
        "semantic_definition": (
            "Process-specific raw and normalized closure among observed formal-process cases only."
        ),
        "definition_status": "candidate",
        "human_validation_gate": "venue-specific entry, closure text/category/time adjudication",
    },
]

TAG_ALIAS_VERSION = "wikimedia-dispute-template-family-v1"


def normalize_tag_family(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    name = value.strip().removeprefix("{{").removesuffix("}}").split("|", 1)[0]
    name = re.sub(r"^(?:subst:|msg:)", "", name, flags=re.IGNORECASE)
    normalized = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    compact = normalized.replace(" ", "")
    if "totallydisputed" in compact:
        return "totally_disputed"
    if "npov" in compact or "neutrality" in compact or "neutralpointofview" in compact:
        return "neutral_point_of_view"
    if "disputed" in compact or "dispute" in compact:
        return "disputed"
    if "accuracy" in compact or "factual" in compact:
        return "factual_accuracy"
    return "other_dispute_template"


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for parser in (
        dt.datetime.fromisoformat,
        lambda item: dt.datetime.strptime(item, "%d/%m/%y %H:%M "),
    ):
        try:
            parsed = parser(text)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(dt.UTC).replace(tzinfo=None)
            return parsed
        except ValueError:
            pass
    return None


def _closure_category(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    lowered = value.casefold()
    if "fail" in lowered:
        return "failure"
    if "success" in lowered or "resolved" in lowered:
        return "success"
    if "withdraw" in lowered:
        return "withdrawn"
    if "general" in lowered:
        return "general"
    return "other"


def _event(
    episode_uid: str,
    event_type: str,
    subtype: str,
    timestamp: Any,
    source_case_uid: str,
    evidence_pointer: str,
    **extra: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    uid = "wdevent:v1:" + canonical_json_hash(
        [episode_uid, event_type, subtype, timestamp, source_case_uid]
    )
    parsed = _parse_time(timestamp)
    event = {
        "event_uid": uid,
        "episode_uid": episode_uid,
        "event_type": event_type,
        "event_subtype": subtype,
        "event_time_exact": timestamp if isinstance(timestamp, str) else None,
        "event_time_utc": parsed.isoformat() if parsed else None,
        "event_time_status": "parsed" if parsed else "unknown_or_unparsed",
        "extraction_method": "pinned_wikidisputes_dispute_metadata",
        "availability_status": "source_available",
        "leakage_class": "raw_event_pending_index_classification",
        "schema_version": SCHEMA_VERSION,
        **extra,
    }
    evidence_uid = "wdevidence:v1:" + canonical_json_hash([uid, evidence_pointer])
    evidence = {
        "event_uid": uid,
        "evidence_uid": evidence_uid,
        "evidence_kind": "wikidisputes_dispute_metadata",
        "source_entity_uid": source_case_uid,
        "evidence_pointer": evidence_pointer,
        "evidence_sha256": None,
    }
    return event, evidence


def materialize_events_and_dvs(output_root: Path) -> dict[str, Any]:
    disputes = pq.read_table(output_root / "silver" / "disputes.parquet").to_pylist()
    episodes = pq.read_table(output_root / "silver" / "dispute_episodes.parquet").to_pylist()
    projection = pq.read_table(
        output_root / "canonical" / "wikidisputes_source_projection.parquet",
        columns=["source_case_uid", "wikidisputes_time"],
    ).to_pylist()
    episode_by_dispute = {row["dispute_uid"]: row for row in episodes}
    case_end: dict[str, dt.datetime | None] = defaultdict(lambda: None)
    for row in projection:
        parsed = _parse_time(row["wikidisputes_time"])
        current = case_end[row["source_case_uid"]]
        if parsed is not None and (current is None or parsed > current):
            case_end[row["source_case_uid"]] = parsed
    events: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    source_events_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for dispute in disputes:
        episode = episode_by_dispute[dispute["dispute_uid"]]
        episode_uid = episode["episode_uid"]
        metadata = json.loads(dispute["dispute_json_canonical"])
        pointer = f"source_case:{dispute['source_case_uid']}:dispute"
        if dispute["source_wikidisputes_escalated"]:
            filing_time = metadata.get("timestamp")
            filing_event, filing_evidence = _event(
                episode_uid,
                "formal_process",
                "drn_filing",
                filing_time,
                dispute["source_case_uid"],
                pointer,
                venue="DRN",
                source_url_exact=metadata.get("url"),
                source_timestamp_semantics="source_process_timestamp_not_independently_validated",
                definition_status="candidate",
            )
            events.append(filing_event)
            evidence.append(filing_evidence)
            source_events_by_episode[episode_uid].append(filing_event)
            mediator = metadata.get("mediator")
            if isinstance(mediator, str) and mediator:
                accepted_event, accepted_evidence = _event(
                    episode_uid,
                    "formal_process",
                    "accepted_mediation",
                    filing_time,
                    dispute["source_case_uid"],
                    pointer,
                    venue="DRN",
                    mediator_exact=mediator,
                    source_url_exact=metadata.get("url"),
                    source_timestamp_semantics=(
                        "source_process_timestamp_not_independently_validated_as_acceptance_time"
                    ),
                    definition_status="candidate",
                )
                events.append(accepted_event)
                evidence.append(accepted_evidence)
                source_events_by_episode[episode_uid].append(accepted_event)
            closure = metadata.get("outcome")
            if isinstance(closure, str) and closure:
                closure_event, closure_evidence = _event(
                    episode_uid,
                    "formal_process_closure",
                    "drn_closure_source",
                    None,
                    dispute["source_case_uid"],
                    pointer,
                    venue="DRN",
                    raw_closure_text_exact=closure,
                    normalized_closure_category=_closure_category(closure),
                    source_url_exact=metadata.get("url"),
                    closure_time_status="not_provided_by_source",
                    definition_status="candidate",
                )
                events.append(closure_event)
                evidence.append(closure_evidence)
                source_events_by_episode[episode_uid].append(closure_event)
        else:
            tag_add, tag_add_evidence = _event(
                episode_uid,
                "dispute_tag",
                "tag_addition",
                metadata.get("start_timestamp"),
                dispute["source_case_uid"],
                pointer,
                tag_name_exact=metadata.get("tag_name"),
                tag_family_normalized=normalize_tag_family(metadata.get("tag_name")),
                tag_alias_version=TAG_ALIAS_VERSION,
                scope="section" if metadata.get("sec_name") else "page",
                section_name_exact=metadata.get("sec_name"),
                title_at_event_exact=metadata.get("pagetitle"),
            )
            tag_remove, tag_remove_evidence = _event(
                episode_uid,
                "dispute_tag",
                "tag_removal",
                metadata.get("end_timestamp"),
                dispute["source_case_uid"],
                pointer,
                tag_name_exact=metadata.get("tag_name"),
                tag_family_normalized=normalize_tag_family(metadata.get("tag_name")),
                tag_alias_version=TAG_ALIAS_VERSION,
                scope="section" if metadata.get("sec_name") else "page",
                section_name_exact=metadata.get("sec_name"),
                title_at_event_exact=metadata.get("pagetitle"),
            )
            events.extend((tag_add, tag_remove))
            evidence.extend((tag_add_evidence, tag_remove_evidence))
            source_events_by_episode[episode_uid].extend((tag_add, tag_remove))

    horizons_by_definition = {
        "formal_escalation_drn": [30, 90, 365, None],
        "durable_dispute_tag_clearance": [30, 90, 365],
        "post_discussion_revert_stability": [7, 30, 90],
        "formal_process_closure_outcome": [None],
    }
    article_histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    article_windows: dict[str, dict[str, Any]] = {}
    article_revision_path = output_root / "silver" / "article_revision_observations.parquet"
    article_window_path = output_root / "silver" / "article_history_windows.parquet"
    if article_revision_path.exists() and article_window_path.exists():
        deduplicated_revisions: dict[tuple[int, int], dict[str, Any]] = {}
        for row in pq.read_table(article_revision_path).to_pylist():
            if isinstance(row.get("page_id"), int) and isinstance(row.get("revision_id"), int):
                deduplicated_revisions.setdefault((row["page_id"], row["revision_id"]), row)
        for row in deduplicated_revisions.values():
            for title in json.loads(row["requested_titles_json"]):
                article_histories[str(title)].append(row)
        for row in pq.read_table(article_window_path).to_pylist():
            for title in json.loads(row["requested_titles_json"]):
                article_windows[str(title)] = row
        for rows in article_histories.values():
            rows.sort(key=lambda row: (str(row.get("timestamp")), int(row["revision_id"])))
    article_reverts: dict[str, list[tuple[IdentityRevert, dict[str, Any]]]] = defaultdict(list)
    for title, rows in article_histories.items():
        by_revision = {str(row["revision_id"]): row for row in rows}
        for revert in detect_identity_reverts(rows):
            reverting = by_revision.get(revert.reverting_revision_id)
            if reverting:
                article_reverts[title].append((revert, reverting))
    dispute_by_uid = {row["dispute_uid"]: row for row in disputes}
    for episode in episodes:
        episode_uid = episode["episode_uid"]
        dispute = dispute_by_uid[episode["dispute_uid"]]
        index = case_end[dispute["source_case_uid"]]
        episode["source_projection_end_at"] = index.isoformat() if index else None
        episode["episode_index_at"] = index.isoformat() if index else None
        episode["dv_observation_start_at"] = index.isoformat() if index else None
        episode["cutoff_rule_version"] = "source-projection-end-v1"
        relevant = source_events_by_episode.get(episode_uid, [])
        dispute_metadata = json.loads(dispute["dispute_json_canonical"])
        pages = dispute_metadata.get("pages")
        article_title = (
            str(pages[0])
            if isinstance(pages, list) and len(pages) == 1
            else str(dispute_metadata.get("pagetitle"))
            if dispute_metadata.get("pagetitle")
            else None
        )
        episode_revert_events: list[dict[str, Any]] = []
        for revert, reverting_revision in article_reverts.get(article_title or "", []):
            event_time = _parse_time(reverting_revision.get("timestamp"))
            if event_time is None or index is None or event_time <= index:
                continue
            event_uid = "wdevent:v1:" + canonical_json_hash(
                [episode_uid, "article_revert", revert.reverting_revision_id]
            )
            event = {
                "event_uid": event_uid,
                "episode_uid": episode_uid,
                "event_type": "article_revert",
                "event_subtype": "sha1_identity_revert",
                "event_time_exact": reverting_revision.get("timestamp"),
                "event_time_utc": event_time.isoformat(),
                "event_time_status": "parsed",
                "page_id": str(reverting_revision["page_id"])
                if reverting_revision.get("page_id") is not None
                else None,
                "revision_id": str(reverting_revision["revision_id"])
                if reverting_revision.get("revision_id") is not None
                else None,
                "reverting_actor_exact": reverting_revision.get("actor_name_exact"),
                "restored_revision_id": revert.restored_revision_id,
                "reverted_revision_ids_json": json.dumps(revert.reverted_revision_ids),
                "sha1": revert.sha1,
                "revert_tags_json": reverting_revision.get("tags_json"),
                "extraction_method": "deterministic_revision_sha1_identity_v1",
                "availability_status": "observed",
                "leakage_class": "post_index",
                "schema_version": SCHEMA_VERSION,
            }
            evidence_uid = "wdevidence:v1:" + canonical_json_hash(
                [event_uid, reverting_revision["article_revision_observation_uid"]]
            )
            events.append(event)
            evidence.append(
                {
                    "event_uid": event_uid,
                    "evidence_uid": evidence_uid,
                    "evidence_kind": "mediawiki_article_revision_sha1_history",
                    "source_entity_uid": reverting_revision["article_revision_observation_uid"],
                    "evidence_pointer": (
                        "article_revision_observation:"
                        + reverting_revision["article_revision_observation_uid"]
                    ),
                    "evidence_sha256": reverting_revision.get("response_content_sha256"),
                }
            )
            episode_revert_events.append(event)
        for definition_id, horizons in horizons_by_definition.items():
            for horizon in horizons:
                applicability = "applicable"
                observation = "unknown"
                value: bool | str | dict[str, Any] | None = None
                event_time = None
                evidence_uids: list[str] = []
                observed_through = None
                extraction_method = "deterministic_source_event_rules_v1"
                reason = "required external history not hydrated"
                if definition_id == "formal_escalation_drn":
                    if not dispute["source_wikidisputes_escalated"]:
                        observation = "not_observable"
                        reason = (
                            "DRN venue/history coverage not yet established; absence is not "
                            "negative"
                        )
                    else:
                        formal = next(
                            (row for row in relevant if row["event_subtype"] == "drn_filing"),
                            None,
                        )
                        parsed = _parse_time(formal["event_time_exact"]) if formal else None
                        if formal and parsed and index and parsed > index:
                            within = horizon is None or parsed <= index + dt.timedelta(days=horizon)
                            observation = "observed"
                            value = within
                            event_time = formal["event_time_utc"]
                            evidence_uids = [formal["event_uid"]]
                            reason = "observed source DRN event after index"
                        elif formal and parsed and index and parsed <= index:
                            observation = "observed_prevalent_or_concurrent"
                            value = None
                            event_time = formal["event_time_utc"]
                            evidence_uids = [formal["event_uid"]]
                            reason = "source event does not qualify as future outcome"
                        else:
                            observation = "unknown"
                            reason = "event or index time unparsed"
                elif definition_id == "durable_dispute_tag_clearance":
                    if dispute["source_wikidisputes_escalated"]:
                        applicability = "not_applicable"
                        observation = "not_observable"
                        reason = "source episode is DRN-aligned rather than tag-sampled"
                    else:
                        removal = next(
                            (row for row in relevant if row["event_subtype"] == "tag_removal"),
                            None,
                        )
                        parsed = _parse_time(removal["event_time_exact"]) if removal else None
                        if removal and parsed and index and parsed > index:
                            observation = "unknown"
                            event_time = removal["event_time_utc"]
                            evidence_uids = [removal["event_uid"]]
                            reason = "removal observed but recurrence/absence window not observed"
                        else:
                            observation = "unknown"
                            reason = (
                                "removal is pre/concurrent, unparsed, or recurrence unavailable"
                            )
                elif definition_id == "post_discussion_revert_stability":
                    window = article_windows.get(article_title or "")
                    window_end = _parse_time(window.get("observation_end_at")) if window else None
                    required_end = index + dt.timedelta(days=horizon) if index and horizon else None
                    covered = bool(
                        window
                        and window.get("observation_status") == "complete_api_window"
                        and required_end
                        and window_end
                        and window_end >= required_end
                    )
                    if covered and index and required_end:
                        qualifying = [
                            row
                            for row in episode_revert_events
                            if (parsed := _parse_time(row.get("event_time_utc"))) is not None
                            and index < parsed <= required_end
                        ]
                        history_rows = article_histories.get(article_title or "", [])
                        actor_by_revision = {
                            str(row["revision_id"]): row.get("actor_name_exact")
                            for row in history_rows
                        }
                        source_participants = {
                            str(value).casefold()
                            for value in dispute_metadata.get("users", [])
                            if value
                        }
                        for side in ("before", "after"):
                            side_value = dispute_metadata.get(side)
                            if isinstance(side_value, dict) and side_value.get("username"):
                                source_participants.add(str(side_value["username"]).casefold())
                        participant_reverts = 0
                        mutual_reverts = 0
                        reverted_count = 0
                        for row in qualifying:
                            reverted_ids = json.loads(row["reverted_revision_ids_json"])
                            reverted_count += len(reverted_ids)
                            actor = row.get("reverting_actor_exact")
                            actor_is_participant = bool(
                                actor and str(actor).casefold() in source_participants
                            )
                            if actor_is_participant:
                                participant_reverts += 1
                                if any(
                                    actor_by_revision.get(str(revision_id))
                                    and str(actor_by_revision[str(revision_id)]).casefold()
                                    in source_participants
                                    for revision_id in reverted_ids
                                ):
                                    mutual_reverts += 1
                        first_time = min(
                            (_parse_time(row["event_time_utc"]) for row in qualifying),
                            default=None,
                        )
                        observation = "observed"
                        value = {
                            "any_revert": bool(qualifying),
                            "reverting_revision_count": len(qualifying),
                            "reverted_revision_count": reverted_count,
                            "time_to_first_revert_seconds": (
                                (first_time - index).total_seconds() if first_time else None
                            ),
                            "participant_involved_reverts": participant_reverts,
                            "mutual_participant_reverts": mutual_reverts,
                            "scope": "article_level_sensitivity",
                            "contested_section_status": "not_observable",
                        }
                        event_time = first_time.isoformat() if first_time else None
                        evidence_uids = [str(window["article_history_window_uid"])] + [
                            row["event_uid"] for row in qualifying
                        ]
                        observed_through = required_end.isoformat()
                        extraction_method = "deterministic_revision_sha1_identity_v1"
                        reason = "complete metadata/SHA-1 window observed"
                    else:
                        observation = "not_observable"
                        reason = (
                            "article SHA-1 history window not completely hydrated; "
                            "missing evidence is not zero"
                        )
                else:
                    if not dispute["source_wikidisputes_escalated"]:
                        applicability = "not_applicable"
                        observation = "not_observable"
                        reason = "no applicable formal process entry in source evidence"
                    else:
                        formal = next(
                            (
                                row
                                for row in relevant
                                if row["event_type"] == "formal_process_closure"
                            ),
                            None,
                        )
                        if formal:
                            observation = "observed_category_time_unknown"
                            value = formal["normalized_closure_category"]
                            evidence_uids = [formal["event_uid"]]
                            reason = "raw closure category available; closure timestamp absent"
                outcomes.append(
                    {
                        "episode_uid": episode_uid,
                        "definition_id": definition_id,
                        "definition_version": DV_VERSION,
                        "definition_status": "candidate",
                        "extraction_build_version": SCHEMA_VERSION,
                        "index_cutoff_version": episode["cutoff_rule_version"],
                        "episode_index_at": episode["episode_index_at"],
                        "horizon_days": horizon,
                        "applicability_status": applicability,
                        "observation_status": observation,
                        "observed_value_json": json.dumps(value),
                        "event_time_utc": event_time,
                        "observed_through_at": observed_through,
                        "censoring_reason": reason
                        if observation in {"unknown", "not_observable"}
                        else None,
                        "evidence_uids_json": json.dumps(evidence_uids),
                        "evidence_coverage": (
                            "complete_article_history_window"
                            if extraction_method == "deterministic_revision_sha1_identity_v1"
                            else "source_evidence"
                            if evidence_uids
                            else "none"
                        ),
                        "extraction_method": extraction_method,
                        "confidence": "candidate_unvalidated",
                        "manual_validation_status": "not_reviewed",
                        "adjudication": None,
                    }
                )

    # Classify raw event timing without dropping prevalent/concurrent evidence.
    index_by_episode = {
        row["episode_uid"]: _parse_time(row["episode_index_at"]) for row in episodes
    }
    for event in events:
        event_time = _parse_time(event["event_time_exact"])
        index = index_by_episode[event["episode_uid"]]
        if event_time is None or index is None:
            event["leakage_class"] = "unknown"
        elif event_time < index:
            event["leakage_class"] = "pre_index"
        elif event_time == index:
            event["leakage_class"] = "at_index"
        else:
            event["leakage_class"] = "post_index"

    # Preserve historical article events as a separate source and in the unified timeline.
    historical_events_path = output_root / "silver" / "events_historical_article_edits.parquet"
    historical_evidence_path = (
        output_root / "silver" / "event_evidence_historical_article_edits.parquet"
    )
    source_event_table = table_from_union_pylist(events)
    source_evidence_table = table_from_union_pylist(evidence)
    if historical_events_path.exists():
        historical = pq.read_table(historical_events_path)
        source_event_table = pa.concat_tables(
            [source_event_table, historical], promote_options="default"
        )
    if historical_evidence_path.exists():
        historical_evidence = pq.read_table(historical_evidence_path)
        source_evidence_table = pa.concat_tables(
            [source_evidence_table, historical_evidence], promote_options="default"
        )

    artifacts: dict[str, Any] = {}
    for name, table in (
        ("dispute_episodes", pa.Table.from_pylist(episodes)),
        ("events", source_event_table),
        ("event_evidence", source_evidence_table),
        ("dv_definitions", pa.Table.from_pylist(DV_DEFINITIONS)),
        ("outcomes", pa.Table.from_pylist(outcomes)),
    ):
        target = output_root / "silver" / f"{name}.parquet"
        atomic_parquet(target, table)
        artifacts[name] = {**file_descriptor(target), "rows": table.num_rows}

    dv_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for outcome in outcomes:
        key = f"{outcome['definition_id']}:{outcome['horizon_days']}"
        dv_counts[key][outcome["observation_status"]] += 1
        dv_counts[key][f"applicability_{outcome['applicability_status']}"] += 1
    report = {
        "artifacts": artifacts,
        "source_dispute_event_count": len(events),
        "dv_counts": {key: dict(value) for key, value in dv_counts.items()},
        "definition_status": "candidate",
        "human_validation_status": "not_reviewed",
        "interpretation_guard": (
            "No value establishes consensus/resolution; missing coverage is never encoded "
            "as negative."
        ),
    }
    atomic_write_json(output_root / "reports" / "events_and_dvs.json", report)
    return report
