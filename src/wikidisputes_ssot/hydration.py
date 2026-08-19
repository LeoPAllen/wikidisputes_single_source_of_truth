from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .config import Settings
from .constants import REPRESENTATION_VERSION, SCHEMA_VERSION
from .hashing import canonical_json_hash, sha256_bytes
from .io import atomic_link_or_copy, atomic_parquet, atomic_write_json, file_descriptor
from .mediawiki import MediaWikiClient, revision_availability

_HYDRATION_WORKER = threading.local()


def _table_with_union_columns(rows: list[dict[str, Any]]) -> pa.Table:
    columns = sorted({column for row in rows for column in row})
    return pa.Table.from_pylist([{column: row.get(column) for column in columns} for row in rows])


def _worker_client(settings: Settings) -> MediaWikiClient:
    client = getattr(_HYDRATION_WORKER, "client", None)
    if not isinstance(client, MediaWikiClient):
        client = MediaWikiClient(settings)
        _HYDRATION_WORKER.client = client
    return client


def _parse_request(
    job: tuple[Settings, int],
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None, str | None]:
    settings, revision_id = job
    try:
        response, manifest = _worker_client(settings).parse_revision(revision_id)
        return revision_id, response, manifest, None
    except RuntimeError as exc:
        return revision_id, None, None, f"{type(exc).__name__}: {exc}"


def _bounded_parse_requests(
    settings: Settings, revision_ids: list[int]
) -> Iterator[tuple[int, dict[str, Any] | None, dict[str, Any] | None, str | None]]:
    jobs = [(settings, revision_id) for revision_id in revision_ids]
    submit_window = settings.network.max_concurrency * 4
    with ThreadPoolExecutor(max_workers=settings.network.max_concurrency) as executor:
        for offset in range(0, len(jobs), submit_window):
            yield from executor.map(_parse_request, jobs[offset : offset + submit_window])


def selected_revision_ids(wikiconv_path: Path) -> list[int]:
    ids: set[int] = set()
    for row in pq.read_table(wikiconv_path).to_pylist():
        meta = json.loads(row["meta_json_canonical"])
        values: list[Any] = [meta]
        original = meta.get("original")
        if isinstance(original, dict):
            values.append(original.get("meta_dict", {}))
        for key in ("modification", "deletion", "restoration"):
            actions = meta.get(key)
            if isinstance(actions, list):
                values.extend(
                    item.get("meta_dict", {}) for item in actions if isinstance(item, dict)
                )
        for value in values:
            if not isinstance(value, dict):
                continue
            revision_id = value.get("rev_id")
            with suppress(TypeError, ValueError):
                ids.add(int(revision_id))
    return sorted(ids)


def _revision_content(revision: dict[str, Any]) -> str | None:
    slots = revision.get("slots")
    if isinstance(slots, dict):
        main = slots.get("main")
        if isinstance(main, dict):
            for key in ("content", "*"):
                if isinstance(main.get(key), str):
                    return str(main[key])
    for key in ("content", "*"):
        if isinstance(revision.get(key), str):
            return str(revision[key])
    return None


def _revision_actor_identity_status(
    actor_name: Any, actor_user_id: Any, *, userhidden: bool
) -> str:
    if userhidden:
        return "revision_actor_hidden_or_deleted"
    if not isinstance(actor_name, str) or not actor_name:
        return "revision_actor_not_observed"
    try:
        ip_address(actor_name)
    except ValueError:
        pass
    else:
        return "revision_actor_ip_observed"
    if actor_name.startswith("~"):
        return "revision_actor_temporary_observed"
    if actor_user_id is None:
        return "revision_actor_name_observed_numeric_id_unavailable"
    return "revision_actor_observed_rename_status_unchecked"


def hydrate_selected_revisions(
    settings: Settings,
    *,
    include_content: bool = True,
    max_revisions: int | None = None,
) -> dict[str, Any]:
    merged = settings.roots.output / "silver" / "wikiconv_selected_rows.parquet"
    revision_ids = selected_revision_ids(merged)
    if max_revisions is not None:
        revision_ids = revision_ids[:max_revisions]
    actions_path = settings.roots.output / "silver" / "utterance_actions.parquet"
    actions = pq.read_table(actions_path).to_pylist()
    action_by_revision: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        revision = action.get("revision_id")
        if revision is not None:
            action_by_revision.setdefault(str(revision), []).append(action)
    context_actions_path = settings.roots.output / "silver" / "context_actions.parquet"
    context_actions = (
        pq.read_table(context_actions_path).to_pylist() if context_actions_path.exists() else []
    )
    context_action_by_revision: dict[str, list[dict[str, Any]]] = {}
    for action in context_actions:
        revision = action.get("revision_id")
        if revision is not None:
            context_action_by_revision.setdefault(str(revision), []).append(action)

    client = MediaWikiClient(settings)
    observations: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    context_representations: list[dict[str, Any]] = []
    actors: list[dict[str, Any]] = []
    returned_ids: set[int] = set()
    request_manifest_by_revision: dict[int, dict[str, Any]] = {}
    request_count = 0
    for response, manifest in client.revisions_by_ids(
        revision_ids, include_content=include_content, batch_size=50
    ):
        request_count += 1
        for requested in str(manifest["normalized_parameters"].get("revids", "")).split("|"):
            with suppress(TypeError, ValueError):
                request_manifest_by_revision[int(requested)] = manifest
        pages = response.get("query", {}).get("pages", [])
        if not isinstance(pages, list):
            pages = []
        for page_index, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            revisions = page.get("revisions")
            if not isinstance(revisions, list):
                revisions = []
            if not revisions:
                observations.append(
                    {
                        "revision_observation_uid": "wdrevision-observation:v1:"
                        + canonical_json_hash([manifest["request_hash"], page_index, None]),
                        "revision_id": None,
                        "page_id": page.get("pageid"),
                        "title_at_retrieval": page.get("title"),
                        **revision_availability(page, None),
                        "request_hash": manifest["request_hash"],
                        "response_blob_path": manifest["blob_path"],
                        "response_content_sha256": manifest["content_sha256"],
                        "response_json_pointer": f"/query/pages/{page_index}",
                        "retrieved_at_utc": manifest["retrieved_at_utc"],
                        "schema_version": SCHEMA_VERSION,
                    }
                )
            for revision_index, revision in enumerate(revisions):
                if not isinstance(revision, dict):
                    continue
                revision_id = revision.get("revid")
                if isinstance(revision_id, int):
                    returned_ids.add(revision_id)
                availability = revision_availability(page, revision)
                content = _revision_content(revision)
                observation_uid = "wdrevision-observation:v1:" + canonical_json_hash(
                    [manifest["request_hash"], page_index, revision_index, revision_id]
                )
                observations.append(
                    {
                        "revision_observation_uid": observation_uid,
                        "revision_id": revision_id,
                        "parent_revision_id": revision.get("parentid"),
                        "page_id": page.get("pageid"),
                        "title_at_retrieval": page.get("title"),
                        "timestamp": revision.get("timestamp"),
                        "actor_name_exact": revision.get("user"),
                        "actor_user_id": revision.get("userid"),
                        "size": revision.get("size"),
                        "sha1": revision.get("sha1"),
                        "content_model": revision.get("contentmodel"),
                        "edit_summary_exact": revision.get("comment"),
                        "tags_json": json.dumps(revision.get("tags"), ensure_ascii=False),
                        **availability,
                        "request_hash": manifest["request_hash"],
                        "response_blob_path": manifest["blob_path"],
                        "response_content_sha256": manifest["content_sha256"],
                        "response_json_pointer": (
                            f"/query/pages/{page_index}/revisions/{revision_index}"
                        ),
                        "retrieved_at_utc": manifest["retrieved_at_utc"],
                        "utterance_action_uids_json": json.dumps(
                            sorted(
                                str(row["action_uid"])
                                for row in action_by_revision.get(str(revision_id), [])
                            )
                        ),
                        "context_action_uids_json": json.dumps(
                            sorted(
                                str(row["context_action_uid"])
                                for row in context_action_by_revision.get(str(revision_id), [])
                            )
                        ),
                        "schema_version": SCHEMA_VERSION,
                    }
                )
                for action in action_by_revision.get(str(revision_id), []):
                    logical_uid = str(action["logical_utterance_uid"])
                    version_uid = str(action["version_uid"])
                    if content is not None:
                        representations.append(
                            {
                                "representation_uid": "wdrepr:v1:"
                                + canonical_json_hash(
                                    [version_uid, observation_uid, "revision_wikitext_raw"]
                                ),
                                "logical_utterance_uid": logical_uid,
                                "version_uid": version_uid,
                                "source_row_uid": action.get("source_row_uid"),
                                "representation_kind": "revision_wikitext_raw",
                                "representation_scope": "full_page_revision",
                                "content_sha256": sha256_bytes(content.encode("utf-8")),
                                "byte_length": len(content.encode("utf-8")),
                                "encoding": "utf-8",
                                "mime_type": "text/x-wiki",
                                "content_inline": None,
                                "blob_path": manifest["blob_path"],
                                "source_revision_id": str(revision_id),
                                "response_json_pointer": (
                                    f"/query/pages/{page_index}/revisions/{revision_index}"
                                    "/slots/main/content"
                                ),
                                "extraction_method": "mediawiki_revisions_api_pinned_oldid",
                                "extraction_version": "1.0.0",
                                "availability_status": availability["availability_status"],
                                "leakage_class": "retrieval_time_only",
                                "available_at": revision.get("timestamp"),
                                "retrieved_at_utc": manifest["retrieved_at_utc"],
                                "confidence": "exact_api_revision_content",
                                "representation_version": REPRESENTATION_VERSION,
                            }
                        )
                    actors.append(
                        {
                            "author_actor_uid": "wdauthor:v1:"
                            + canonical_json_hash([version_uid, observation_uid]),
                            "logical_utterance_uid": logical_uid,
                            "version_uid": version_uid,
                            "source_row_uid": action.get("source_row_uid"),
                            "wikidisputes_user_exact": None,
                            "wikiconv_speaker_exact": None,
                            "revision_actor_name_exact": revision.get("user"),
                            "revision_actor_user_id": revision.get("userid"),
                            "identity_status": _revision_actor_identity_status(
                                revision.get("user"),
                                revision.get("userid"),
                                userhidden=bool(availability["userhidden"]),
                            ),
                            "resolved_identity": None,
                            "resolution_method": None,
                            "confidence": "exact_revision_metadata",
                        }
                    )
                for action in context_action_by_revision.get(str(revision_id), []):
                    if content is None:
                        continue
                    context_version_uid = str(action["context_version_uid"])
                    context_representations.append(
                        {
                            "context_representation_uid": "wdcontextrepr:v1:"
                            + canonical_json_hash(
                                [
                                    context_version_uid,
                                    observation_uid,
                                    "revision_wikitext_raw",
                                ]
                            ),
                            "context_node_uid": action["context_node_uid"],
                            "context_version_uid": context_version_uid,
                            "representation_kind": "revision_wikitext_raw",
                            "representation_scope": "full_page_revision",
                            "content_sha256": sha256_bytes(content.encode("utf-8")),
                            "byte_length": len(content.encode("utf-8")),
                            "encoding": "utf-8",
                            "mime_type": "text/x-wiki",
                            "content_inline": None,
                            "blob_path": manifest["blob_path"],
                            "source_revision_id": str(revision_id),
                            "response_json_pointer": (
                                f"/query/pages/{page_index}/revisions/{revision_index}"
                                "/slots/main/content"
                            ),
                            "extraction_method": "mediawiki_revisions_api_pinned_oldid",
                            "extraction_version": "1.0.0",
                            "availability_status": availability["availability_status"],
                            "available_at": revision.get("timestamp"),
                            "retrieved_at_utc": manifest["retrieved_at_utc"],
                            "evidence_pointer": f"revision_observation:{observation_uid}",
                            "representation_version": REPRESENTATION_VERSION,
                        }
                    )

    missing_ids = sorted(set(revision_ids) - returned_ids)
    for revision_id in missing_ids:
        manifest = request_manifest_by_revision.get(revision_id, {})
        observations.append(
            {
                "revision_observation_uid": "wdrevision-observation:v1:"
                + canonical_json_hash(["not_returned", revision_id]),
                "revision_id": revision_id,
                "availability_status": "revision_not_returned",
                "userhidden": False,
                "sha1hidden": False,
                "commenthidden": False,
                "texthidden": False,
                "page_missing": False,
                "revision_missing": True,
                "request_hash": manifest.get("request_hash"),
                "response_blob_path": manifest.get("blob_path"),
                "response_content_sha256": manifest.get("content_sha256"),
                "retrieved_at_utc": manifest.get("retrieved_at_utc"),
                "schema_version": SCHEMA_VERSION,
            }
        )

    silver = settings.roots.output / "silver"
    observation_path = silver / "talk_page_revision_observations.parquet"
    atomic_parquet(observation_path, pa.Table.from_pylist(observations))
    existing_representations = pq.read_table(
        silver / "utterance_representations.parquet"
    ).to_pylist()
    representation_by_uid = {
        str(row["representation_uid"]): row for row in existing_representations + representations
    }
    atomic_parquet(
        silver / "utterance_representations.parquet",
        _table_with_union_columns(list(representation_by_uid.values())),
    )
    existing_actors = pq.read_table(silver / "authors_actors.parquet").to_pylist()
    actor_by_uid = {str(row["author_actor_uid"]): row for row in existing_actors + actors}
    atomic_parquet(
        silver / "authors_actors.parquet",
        _table_with_union_columns(list(actor_by_uid.values())),
    )
    if context_representations:
        context_representation_path = silver / "context_representations.parquet"
        existing_context_representations = pq.read_table(context_representation_path).to_pylist()
        context_by_uid = {
            str(row["context_representation_uid"]): row
            for row in existing_context_representations + context_representations
        }
        atomic_parquet(
            context_representation_path,
            _table_with_union_columns(list(context_by_uid.values())),
        )
    report = {
        "requested_revision_ids": len(revision_ids),
        "returned_revision_ids": len(returned_ids),
        "missing_revision_ids": len(missing_ids),
        "request_count": request_count,
        "include_content": include_content,
        "representation_rows_added": len(representations),
        "actor_rows_added": len(actors),
        "context_representation_rows_added": len(context_representations),
        "availability_counts": dict(Counter(row["availability_status"] for row in observations)),
        "artifact": {
            **file_descriptor(observation_path),
            "rows": len(observations),
        },
        "completeness_status": (
            "complete_enumeration"
            if max_revisions is None and not missing_ids
            else "complete_enumeration_with_explicit_unavailable"
            if max_revisions is None
            else "bounded_pilot"
        ),
    }
    atomic_write_json(settings.roots.output / "reports" / "mediawiki_revisions.json", report)
    return report


def hydrate_selected_parses(
    settings: Settings, *, max_revisions: int | None = None
) -> dict[str, Any]:
    """Parse pinned historical oldids and retain exact reconstructed HTML responses."""
    merged = settings.roots.output / "silver" / "wikiconv_selected_rows.parquet"
    revision_ids = selected_revision_ids(merged)
    if max_revisions is not None:
        revision_ids = revision_ids[:max_revisions]
    actions = pq.read_table(
        settings.roots.output / "silver" / "utterance_actions.parquet"
    ).to_pylist()
    actions_by_revision: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        if action.get("revision_id") is not None:
            actions_by_revision.setdefault(str(action["revision_id"]), []).append(action)
    context_actions_path = settings.roots.output / "silver" / "context_actions.parquet"
    context_actions = (
        pq.read_table(context_actions_path).to_pylist() if context_actions_path.exists() else []
    )
    context_actions_by_revision: dict[str, list[dict[str, Any]]] = {}
    for action in context_actions:
        if action.get("revision_id") is not None:
            context_actions_by_revision.setdefault(str(action["revision_id"]), []).append(action)
    observations: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    context_representations: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    for revision_id, response, manifest, retrieval_error in _bounded_parse_requests(
        settings, revision_ids
    ):
        try:
            if retrieval_error is not None:
                raise RuntimeError(retrieval_error)
            assert response is not None and manifest is not None
            parsed = response.get("parse") if isinstance(response, dict) else None
            if not isinstance(parsed, dict):
                status = "parse_payload_unavailable"
                parsed = {}
            else:
                status = "reconstructed"
            html_text = parsed.get("text")
            if isinstance(html_text, dict):
                html_text = html_text.get("*")
            warnings = parsed.get("parsewarnings")
            observation_uid = "wdparse-observation:v1:" + canonical_json_hash(
                [manifest["request_hash"], revision_id]
            )
            observations.append(
                {
                    "parse_observation_uid": observation_uid,
                    "revision_id": revision_id,
                    "parser_mode": settings.mediawiki.parser,
                    "skin": "vector",
                    "parse_status": status,
                    "parse_warnings_json": json.dumps(warnings, ensure_ascii=False),
                    "request_hash": manifest["request_hash"],
                    "response_blob_path": manifest["blob_path"],
                    "response_content_sha256": manifest["content_sha256"],
                    "response_json_pointer": "/parse/text",
                    "retrieved_at_utc": manifest["retrieved_at_utc"],
                    "schema_version": SCHEMA_VERSION,
                }
            )
            status_counts[status] += 1
            if isinstance(html_text, str):
                for action in actions_by_revision.get(str(revision_id), []):
                    version_uid = str(action["version_uid"])
                    representations.append(
                        {
                            "representation_uid": "wdrepr:v1:"
                            + canonical_json_hash(
                                [version_uid, observation_uid, "rendered_html_reconstructed"]
                            ),
                            "logical_utterance_uid": action["logical_utterance_uid"],
                            "version_uid": version_uid,
                            "source_row_uid": action.get("source_row_uid"),
                            "representation_kind": "rendered_html_reconstructed",
                            "representation_scope": "full_page_revision",
                            "content_sha256": sha256_bytes(html_text.encode("utf-8")),
                            "byte_length": len(html_text.encode("utf-8")),
                            "encoding": "utf-8",
                            "mime_type": "text/html",
                            "content_inline": None,
                            "blob_path": manifest["blob_path"],
                            "source_revision_id": str(revision_id),
                            "response_json_pointer": "/parse/text",
                            "extraction_method": "mediawiki_action_parse_oldid",
                            "extraction_version": "1.0.0",
                            "parser_mode": settings.mediawiki.parser,
                            "skin": "vector",
                            "parse_warnings_json": json.dumps(warnings, ensure_ascii=False),
                            "availability_status": "reconstructed",
                            "leakage_class": "retrieval_time_rendering",
                            "available_at": None,
                            "retrieved_at_utc": manifest["retrieved_at_utc"],
                            "confidence": "exact_returned_html_string",
                            "representation_version": REPRESENTATION_VERSION,
                        }
                    )
                for action in context_actions_by_revision.get(str(revision_id), []):
                    context_version_uid = str(action["context_version_uid"])
                    context_representations.append(
                        {
                            "context_representation_uid": "wdcontextrepr:v1:"
                            + canonical_json_hash(
                                [
                                    context_version_uid,
                                    observation_uid,
                                    "rendered_html_reconstructed",
                                ]
                            ),
                            "context_node_uid": action["context_node_uid"],
                            "context_version_uid": context_version_uid,
                            "representation_kind": "rendered_html_reconstructed",
                            "representation_scope": "full_page_revision",
                            "content_sha256": sha256_bytes(html_text.encode("utf-8")),
                            "byte_length": len(html_text.encode("utf-8")),
                            "encoding": "utf-8",
                            "mime_type": "text/html",
                            "content_inline": None,
                            "blob_path": manifest["blob_path"],
                            "source_revision_id": str(revision_id),
                            "response_json_pointer": "/parse/text",
                            "extraction_method": "mediawiki_action_parse_oldid",
                            "parser_mode": settings.mediawiki.parser,
                            "skin": "vector",
                            "parse_warnings_json": json.dumps(warnings, ensure_ascii=False),
                            "availability_status": "reconstructed",
                            "available_at": None,
                            "retrieved_at_utc": manifest["retrieved_at_utc"],
                            "evidence_pointer": f"parse_observation:{observation_uid}",
                            "representation_version": REPRESENTATION_VERSION,
                        }
                    )
        except RuntimeError as exc:
            status_counts["retrieval_failed"] += 1
            observations.append(
                {
                    "parse_observation_uid": "wdparse-observation:v1:"
                    + canonical_json_hash(["retrieval_failed", revision_id]),
                    "revision_id": revision_id,
                    "parser_mode": settings.mediawiki.parser,
                    "parse_status": "retrieval_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "schema_version": SCHEMA_VERSION,
                }
            )
    silver = settings.roots.output / "silver"
    observations_path = silver / "historical_parse_observations.parquet"
    atomic_parquet(observations_path, _table_with_union_columns(observations))
    existing = pq.read_table(silver / "utterance_representations.parquet").to_pylist()
    by_uid = {str(row["representation_uid"]): row for row in existing + representations}
    atomic_parquet(
        silver / "utterance_representations.parquet",
        _table_with_union_columns(list(by_uid.values())),
    )
    context_representation_path = silver / "context_representations.parquet"
    if context_representations and context_representation_path.exists():
        existing_context = pq.read_table(context_representation_path).to_pylist()
        context_by_uid = {
            str(row["context_representation_uid"]): row
            for row in existing_context + context_representations
        }
        atomic_parquet(
            context_representation_path,
            _table_with_union_columns(list(context_by_uid.values())),
        )
        context_creation_version = {
            str(row["context_node_uid"]): str(row["context_version_uid"])
            for row in context_actions
            if row.get("action_type") == "creation"
        }
        html_by_context = {
            str(row["context_node_uid"]): row["context_representation_uid"]
            for row in context_representations
            if str(row["context_version_uid"])
            == context_creation_version.get(str(row["context_node_uid"]))
        }
        context_nodes_path = silver / "context_nodes.parquet"
        context_nodes = pq.read_table(context_nodes_path).to_pylist()
        for context in context_nodes:
            context["rendered_html_reconstructed_representation_uid"] = html_by_context.get(
                str(context["context_node_uid"])
            )
        atomic_parquet(context_nodes_path, _table_with_union_columns(context_nodes))
    creation_version = {
        str(row["logical_utterance_uid"]): str(row["version_uid"])
        for row in actions
        if row.get("action_type") == "creation"
    }
    html_by_logical = {
        str(row["logical_utterance_uid"]): row["representation_uid"]
        for row in representations
        if str(row["version_uid"]) == creation_version.get(str(row["logical_utterance_uid"]))
    }
    utterance_path = silver / "utterances.parquet"
    utterances = pq.read_table(utterance_path).to_pylist()
    for utterance in utterances:
        utterance["rendered_html_reconstructed_representation_uid"] = html_by_logical.get(
            str(utterance["logical_utterance_uid"])
        )
    atomic_parquet(utterance_path, _table_with_union_columns(utterances))
    atomic_link_or_copy(
        utterance_path,
        settings.roots.output / "canonical" / "wikidisputes_utterances_ssot.parquet",
    )
    report = {
        "requested_revision_ids": len(revision_ids),
        "status_counts": dict(status_counts),
        "rendered_html_representations_added": len(representations),
        "context_rendered_html_representations_added": len(context_representations),
        "max_revisions": max_revisions,
        "completeness_status": (
            "complete_enumeration_with_explicit_failures"
            if max_revisions is None and status_counts["retrieval_failed"]
            else "complete_enumeration"
            if max_revisions is None
            else "bounded_pilot"
        ),
        "semantic_guard": (
            "action=parse output is rendered_html_reconstructed at retrieval time, never "
            "html_archival or original historical HTML"
        ),
        "artifact": {**file_descriptor(observations_path), "rows": len(observations)},
    }
    atomic_write_json(settings.roots.output / "reports" / "mediawiki_parses.json", report)
    return report
