from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from itertools import chain
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .config import Settings
from .constants import SCHEMA_VERSION
from .hashing import canonical_json_hash
from .io import atomic_parquet, atomic_write_json, file_descriptor
from .mediawiki import MediaWikiClient, revision_availability


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _batched(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _title_key(value: str) -> str:
    return value.replace("_", " ").strip().casefold()


def _resolve_title_batch(
    client: MediaWikiClient, titles: list[str]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    response, manifest = client.request(
        {
            "action": "query",
            "prop": "info",
            "titles": "|".join(titles),
            "redirects": "1",
        }
    )
    query = response.get("query", {}) if isinstance(response, dict) else {}
    transitions: dict[str, str] = {}
    for field in ("normalized", "redirects"):
        for mapping in query.get(field, []) if isinstance(query.get(field), list) else []:
            if isinstance(mapping, dict) and mapping.get("from") and mapping.get("to"):
                transitions[_title_key(str(mapping["from"]))] = _title_key(str(mapping["to"]))
    rows: list[dict[str, Any]] = []
    resolved: dict[str, dict[str, Any]] = {}
    pages = query.get("pages", [])
    if not isinstance(pages, list):
        pages = []
    pages_by_key: dict[str, tuple[int, dict[str, Any]]] = {}
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        pages_by_key[_title_key(str(page.get("title", "")))] = (page_index, page)
    for requested_title in titles:
        key = _title_key(requested_title)
        seen: set[str] = set()
        while key in transitions and key not in seen:
            seen.add(key)
            key = transitions[key]
        page_index, page = pages_by_key.get(key, (-1, {"title": requested_title, "missing": True}))
        returned_title = str(page.get("title", requested_title))
        status = "missing_page" if page.get("missing") is True or "missing" in page else "resolved"
        row = {
            "article_page_observation_uid": "wdarticle-page-observation:v1:"
            + canonical_json_hash([manifest["request_hash"], page_index, requested_title]),
            "requested_title_exact": requested_title,
            "returned_title_exact": returned_title,
            "page_id": page.get("pageid"),
            "namespace": page.get("ns"),
            "resolution_status": status,
            "request_hash": manifest["request_hash"],
            "response_blob_path": manifest["blob_path"],
            "response_content_sha256": manifest["content_sha256"],
            "response_json_pointer": f"/query/pages/{page_index}",
            "retrieved_at_utc": manifest["retrieved_at_utc"],
            "schema_version": SCHEMA_VERSION,
        }
        rows.append(row)
        resolved[requested_title] = row
    return rows, resolved


def hydrate_article_histories(
    settings: Settings, *, max_pages: int | None = None
) -> dict[str, Any]:
    """Hydrate metadata-only article histories for prespecified 365-day windows.

    API response bytes are cached by the shared content-addressed client. The
    normalized table stores no article wikitext, minimizing disk use while
    retaining the SHA-1 sequence required for identity-revert detection.
    """
    episodes = pq.read_table(
        settings.roots.output / "silver" / "dispute_episodes.parquet"
    ).to_pylist()
    requested_titles = sorted(
        {
            str(row["title_at_event_exact"])
            for row in episodes
            if row.get("title_at_event_exact") and row.get("episode_index_at")
        },
        key=lambda value: (value.casefold(), value),
    )
    if max_pages is not None:
        requested_titles = requested_titles[:max_pages]
    client = MediaWikiClient(settings)
    page_rows: list[dict[str, Any]] = []
    resolved: dict[str, dict[str, Any]] = {}
    for batch in _batched(requested_titles, 50):
        observations, mapping = _resolve_title_batch(client, batch)
        page_rows.extend(observations)
        resolved.update(mapping)

    windows: dict[int, tuple[dt.datetime, dt.datetime]] = {}
    titles_by_page: dict[int, set[str]] = defaultdict(set)
    for episode in episodes:
        title = episode.get("title_at_event_exact")
        index = _parse_time(episode.get("episode_index_at"))
        page = resolved.get(str(title)) if title else None
        if not page or not isinstance(page.get("page_id"), int) or index is None:
            continue
        page_id = int(page["page_id"])
        start = index
        end = index + dt.timedelta(days=365)
        if page_id in windows:
            prior_start, prior_end = windows[page_id]
            windows[page_id] = min(start, prior_start), max(end, prior_end)
        else:
            windows[page_id] = start, end
        titles_by_page[page_id].add(str(title))

    revision_rows: list[dict[str, Any]] = []
    page_failures: list[dict[str, Any]] = []
    for page_id in sorted(windows):
        start, end = windows[page_id]
        try:
            start_text = start.isoformat().replace("+00:00", "Z")
            baseline = client.revision_at_or_before(page_id, start_text)
            for response, manifest in chain(
                (baseline,),
                client.full_page_history(
                    page_id,
                    start=start_text,
                    end=end.isoformat().replace("+00:00", "Z"),
                ),
            ):
                pages = response.get("query", {}).get("pages", [])
                if not isinstance(pages, list):
                    pages = []
                for page_index, page in enumerate(pages):
                    if not isinstance(page, dict):
                        continue
                    revisions = page.get("revisions", [])
                    if not isinstance(revisions, list):
                        revisions = []
                    for revision_index, revision in enumerate(revisions):
                        if not isinstance(revision, dict):
                            continue
                        availability = revision_availability(page, revision)
                        revision_id = revision.get("revid")
                        observation_uid = (
                            "wdarticle-revision-observation:v1:"
                            + canonical_json_hash(
                                [manifest["request_hash"], page_index, revision_index, revision_id]
                            )
                        )
                        revision_rows.append(
                            {
                                "article_revision_observation_uid": observation_uid,
                                "page_id": page.get("pageid", page_id),
                                "requested_titles_json": json.dumps(
                                    sorted(titles_by_page[page_id]), ensure_ascii=False
                                ),
                                "title_at_retrieval": page.get("title"),
                                "revision_id": revision_id,
                                "parent_revision_id": revision.get("parentid"),
                                "timestamp": revision.get("timestamp"),
                                "actor_name_exact": revision.get("user"),
                                "actor_user_id": revision.get("userid"),
                                "size": revision.get("size"),
                                "sha1": revision.get("sha1"),
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
                                "schema_version": SCHEMA_VERSION,
                            }
                        )
        except RuntimeError as exc:
            page_failures.append(
                {
                    "page_id": page_id,
                    "requested_titles": sorted(titles_by_page[page_id]),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    silver = settings.roots.output / "silver"
    page_path = silver / "article_page_observations.parquet"
    revisions_path = silver / "article_revision_observations.parquet"
    failed_page_ids = {int(row["page_id"]) for row in page_failures}
    window_rows = [
        {
            "article_history_window_uid": "wdarticle-history-window:v1:"
            + canonical_json_hash([page_id, start.isoformat(), end.isoformat()]),
            "page_id": page_id,
            "requested_titles_json": json.dumps(sorted(titles_by_page[page_id])),
            "observation_start_at": start.isoformat(),
            "observation_end_at": end.isoformat(),
            "observation_status": "retrieval_failed"
            if page_id in failed_page_ids
            else "complete_api_window",
            "prehistory_status": "immediate_revision_at_or_before_window_start",
            "schema_version": SCHEMA_VERSION,
        }
        for page_id, (start, end) in sorted(windows.items())
    ]
    windows_path = silver / "article_history_windows.parquet"
    atomic_parquet(
        page_path,
        pa.Table.from_pylist(page_rows)
        if page_rows
        else pa.table({"_empty": pa.array([], pa.string())}),
    )
    atomic_parquet(
        revisions_path,
        pa.Table.from_pylist(revision_rows)
        if revision_rows
        else pa.table({"_empty": pa.array([], pa.string())}),
    )
    atomic_parquet(
        windows_path,
        pa.Table.from_pylist(window_rows)
        if window_rows
        else pa.table({"_empty": pa.array([], pa.string())}),
    )
    report = {
        "requested_titles": len(requested_titles),
        "resolved_pages": len(windows),
        "missing_or_unresolved_titles": sum(
            row["resolution_status"] != "resolved" for row in page_rows
        ),
        "revision_observations": len(revision_rows),
        "page_failures": page_failures,
        "max_pages": max_pages,
        "completeness_status": (
            "complete_with_explicit_failures" if max_pages is None else "bounded_pilot"
        ),
        "storage_policy": "metadata_and_sha1_only; exact compressed API responses retained",
        "artifacts": {
            "pages": {**file_descriptor(page_path), "rows": len(page_rows)},
            "revisions": {**file_descriptor(revisions_path), "rows": len(revision_rows)},
            "windows": {**file_descriptor(windows_path), "rows": len(window_rows)},
        },
    }
    atomic_write_json(settings.roots.output / "reports" / "article_history.json", report)
    return report
