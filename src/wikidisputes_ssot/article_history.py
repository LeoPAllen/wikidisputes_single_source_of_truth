from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from itertools import chain
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .config import Settings
from .constants import SCHEMA_VERSION
from .hashing import canonical_json_hash
from .io import atomic_parquet, atomic_write_json, file_descriptor
from .mediawiki import MediaWikiClient, revision_availability

ARTICLE_REVISION_SCHEMA = pa.schema(
    [
        ("article_revision_observation_uid", pa.string()),
        ("page_id", pa.int64()),
        ("requested_titles_json", pa.string()),
        ("title_at_retrieval", pa.string()),
        ("revision_id", pa.int64()),
        ("parent_revision_id", pa.int64()),
        ("timestamp", pa.string()),
        ("actor_name_exact", pa.string()),
        ("actor_user_id", pa.int64()),
        ("size", pa.int64()),
        ("sha1", pa.string()),
        ("edit_summary_exact", pa.string()),
        ("tags_json", pa.string()),
        ("availability_status", pa.string()),
        ("userhidden", pa.bool_()),
        ("sha1hidden", pa.bool_()),
        ("commenthidden", pa.bool_()),
        ("texthidden", pa.bool_()),
        ("page_missing", pa.bool_()),
        ("revision_missing", pa.bool_()),
        ("request_hash", pa.string()),
        ("response_blob_path", pa.string()),
        ("response_content_sha256", pa.string()),
        ("response_json_pointer", pa.string()),
        ("retrieved_at_utc", pa.string()),
        ("schema_version", pa.string()),
    ]
)
_ARTICLE_WORKER = threading.local()


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


def _worker_client(settings: Settings) -> MediaWikiClient:
    client = getattr(_ARTICLE_WORKER, "client", None)
    if not isinstance(client, MediaWikiClient):
        client = MediaWikiClient(settings)
        _ARTICLE_WORKER.client = client
    return client


def _hydrate_article_page(
    job: tuple[Settings, int, dt.datetime, dt.datetime, tuple[str, ...]],
) -> tuple[int, list[dict[str, Any]], dict[str, Any] | None]:
    settings, page_id, start, end, requested_titles = job
    client = _worker_client(settings)
    response_rows: list[dict[str, Any]] = []
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
                    observation_uid = "wdarticle-revision-observation:v1:" + canonical_json_hash(
                        [manifest["request_hash"], page_index, revision_index, revision_id]
                    )
                    response_rows.append(
                        {
                            "article_revision_observation_uid": observation_uid,
                            "page_id": page.get("pageid", page_id),
                            "requested_titles_json": json.dumps(
                                requested_titles, ensure_ascii=False
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
        return (
            page_id,
            response_rows,
            {
                "page_id": page_id,
                "requested_titles": list(requested_titles),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
    return page_id, response_rows, None


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

    silver = settings.roots.output / "silver"
    silver.mkdir(parents=True, exist_ok=True)
    revisions_path = silver / "article_revision_observations.parquet"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{revisions_path.name}.", dir=revisions_path.parent
    )
    os.close(descriptor)
    temporary_path = type(revisions_path)(temporary_name)
    writer: pq.ParquetWriter | None = pq.ParquetWriter(
        temporary_path,
        ARTICLE_REVISION_SCHEMA,
        compression="zstd",
        compression_level=9,
        use_dictionary=True,
        write_statistics=True,
        data_page_version="2.0",
    )
    revision_observation_count = 0
    page_failures: list[dict[str, Any]] = []
    try:
        jobs = [
            (
                settings,
                page_id,
                windows[page_id][0],
                windows[page_id][1],
                tuple(sorted(titles_by_page[page_id])),
            )
            for page_id in sorted(windows)
        ]
        with ThreadPoolExecutor(max_workers=settings.network.max_concurrency) as executor:
            for _page_id, response_rows, failure in executor.map(_hydrate_article_page, jobs):
                if response_rows:
                    assert writer is not None
                    writer.write_table(
                        pa.Table.from_pylist(response_rows, schema=ARTICLE_REVISION_SCHEMA)
                    )
                    revision_observation_count += len(response_rows)
                if failure is not None:
                    page_failures.append(failure)
        assert writer is not None
        writer.close()
        writer = None
        os.replace(temporary_path, revisions_path)
    except BaseException:
        if writer is not None:
            writer.close()
        with suppress(FileNotFoundError):
            temporary_path.unlink()
        raise

    page_path = silver / "article_page_observations.parquet"
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
        "revision_observations": revision_observation_count,
        "page_failures": page_failures,
        "max_pages": max_pages,
        "completeness_status": (
            "complete_with_explicit_failures" if max_pages is None else "bounded_pilot"
        ),
        "storage_policy": "metadata_and_sha1_only; exact compressed API responses retained",
        "artifacts": {
            "pages": {**file_descriptor(page_path), "rows": len(page_rows)},
            "revisions": {
                **file_descriptor(revisions_path),
                "rows": revision_observation_count,
            },
            "windows": {**file_descriptor(windows_path), "rows": len(window_rows)},
        },
    }
    atomic_write_json(settings.roots.output / "reports" / "article_history.json", report)
    return report
