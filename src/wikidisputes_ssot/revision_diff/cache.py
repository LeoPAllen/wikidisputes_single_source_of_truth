"""Cache-first exact-response plumbing for Method-B revision pairs."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from wikidisputes_ssot.config import Settings
from wikidisputes_ssot.io import atomic_parquet, atomic_write_json, table_from_union_pylist
from wikidisputes_ssot.mediawiki import MediaWikiClient, revision_availability

from .models import RevisionAvailability, RevisionText, local_content_sha256

REVISION_CACHE_VERSION = "method-b-revision-cache-v1"


@dataclass(frozen=True, slots=True)
class CachedRevision:
    revision_id: int
    parent_revision_id: int | None
    page_id: int | None
    title: str | None
    availability_status: str
    api_sha1: str | None
    timestamp: str | None
    actor_name_exact: str | None
    actor_user_id: int | None
    request_hash: str | None
    response_content_sha256: str | None
    response_blob_path: str | None
    response_json_pointer: str | None
    local_content_sha256: str | None
    cache_version: str = REVISION_CACHE_VERSION

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RevisionPair:
    target_revision_id: int
    predecessor_revision_id: int | None
    target_availability: str
    predecessor_availability: str
    parentid_verified: bool
    page_id_consistent: bool | None
    cache_version: str = REVISION_CACHE_VERSION

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def _content_from_revision(revision: Mapping[str, Any]) -> str | None:
    slots = revision.get("slots")
    if isinstance(slots, Mapping):
        main = slots.get("main")
        if isinstance(main, Mapping):
            for key in ("content", "*"):
                if isinstance(main.get(key), str):
                    return str(main[key])
    for key in ("content", "*"):
        if isinstance(revision.get(key), str):
            return str(revision[key])
    return None


def _read_exact_response(settings: Settings, row: Mapping[str, Any]) -> dict[str, Any]:
    relative = row.get("response_blob_path")
    if not relative:
        raise FileNotFoundError("revision observation has no exact response blob pointer")
    path = settings.roots.data / "bronze" / "blobs" / str(relative)
    stored = path.read_bytes()
    body = gzip.decompress(stored) if str(path).endswith(".gz") else stored
    expected = row.get("response_content_sha256")
    actual = hashlib.sha256(body).hexdigest()
    if expected and str(expected) != actual:
        raise RuntimeError(f"exact response hash mismatch for {path}")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("MediaWiki response must be a JSON object")
    return parsed


def _revision_from_response(
    response: Mapping[str, Any], revision_id: int
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    pages = (
        response.get("query", {}).get("pages", [])
        if isinstance(response.get("query"), Mapping)
        else []
    )
    if not isinstance(pages, list):
        return None
    for page in pages:
        if not isinstance(page, dict):
            continue
        revisions = page.get("revisions")
        if not isinstance(revisions, list):
            continue
        for revision in revisions:
            if isinstance(revision, dict) and revision.get("revid") == revision_id:
                return page, revision
    return None


def cached_revision_from_observation(
    settings: Settings, row: Mapping[str, Any]
) -> CachedRevision:
    revision_id = int(row["revision_id"])
    content_hash: str | None = None
    if row.get("availability_status") == "content_available":
        response = _read_exact_response(settings, row)
        found = _revision_from_response(response, revision_id)
        if found is None:
            raise RuntimeError(f"revision {revision_id} missing from its exact response pointer")
        content = _content_from_revision(found[1])
        if content is not None:
            content_hash = local_content_sha256(content)
    return CachedRevision(
        revision_id=revision_id,
        parent_revision_id=int(row["parent_revision_id"])
        if row.get("parent_revision_id") is not None
        else None,
        page_id=int(row["page_id"]) if row.get("page_id") is not None else None,
        title=str(row["title_at_retrieval"]) if row.get("title_at_retrieval") else None,
        availability_status=str(row.get("availability_status") or "unavailable"),
        api_sha1=str(row["sha1"]) if row.get("sha1") else None,
        timestamp=str(row["timestamp"]) if row.get("timestamp") else None,
        actor_name_exact=str(row["actor_name_exact"]) if row.get("actor_name_exact") else None,
        actor_user_id=int(row["actor_user_id"])
        if row.get("actor_user_id") is not None
        else None,
        request_hash=str(row["request_hash"]) if row.get("request_hash") else None,
        response_content_sha256=str(row["response_content_sha256"])
        if row.get("response_content_sha256")
        else None,
        response_blob_path=str(row["response_blob_path"])
        if row.get("response_blob_path")
        else None,
        response_json_pointer=str(row["response_json_pointer"])
        if row.get("response_json_pointer")
        else None,
        local_content_sha256=content_hash,
    )


def cached_revision_from_api(
    response: Mapping[str, Any], manifest: Mapping[str, Any], revision_id: int
) -> CachedRevision:
    found = _revision_from_response(response, revision_id)
    if found is None:
        return CachedRevision(
            revision_id=revision_id,
            parent_revision_id=None,
            page_id=None,
            title=None,
            availability_status="revision_not_returned",
            api_sha1=None,
            timestamp=None,
            actor_name_exact=None,
            actor_user_id=None,
            request_hash=str(manifest.get("request_hash") or "") or None,
            response_content_sha256=str(manifest.get("content_sha256") or "") or None,
            response_blob_path=str(manifest.get("blob_path") or "") or None,
            response_json_pointer=None,
            local_content_sha256=None,
        )
    page, revision = found
    content = _content_from_revision(revision)
    availability = revision_availability(page, revision)["availability_status"]
    return CachedRevision(
        revision_id=revision_id,
        parent_revision_id=revision.get("parentid"),
        page_id=page.get("pageid"),
        title=page.get("title"),
        availability_status=availability,
        api_sha1=revision.get("sha1"),
        timestamp=revision.get("timestamp"),
        actor_name_exact=revision.get("user"),
        actor_user_id=revision.get("userid"),
        request_hash=str(manifest.get("request_hash") or "") or None,
        response_content_sha256=str(manifest.get("content_sha256") or "") or None,
        response_blob_path=str(manifest.get("blob_path") or "") or None,
        response_json_pointer=f"revision:{revision_id}",
        local_content_sha256=local_content_sha256(content) if content is not None else None,
    )


def load_cached_revision_index(
    settings: Settings,
    method_b_index: Path,
    *,
    required_ids: set[int] | None = None,
) -> dict[int, CachedRevision]:
    records: dict[int, CachedRevision] = {}
    generic = settings.roots.output / "silver" / "talk_page_revision_observations.parquet"
    paths = [generic, method_b_index]
    for path in paths:
        if not path.exists():
            continue
        for row in pq.read_table(path).to_pylist():
            if row.get("revision_id") is None:
                continue
            if required_ids is not None and int(row["revision_id"]) not in required_ids:
                continue
            try:
                if "cache_version" in row:
                    record = CachedRevision(
                        **{
                            key: row.get(key)
                            for key in CachedRevision.__dataclass_fields__
                        }
                    )
                else:
                    record = cached_revision_from_observation(settings, row)
            except (FileNotFoundError, RuntimeError, ValueError):
                # Preserve an auditable availability state; never bypass a bad
                # pointer by claiming content from a different representation.
                record = CachedRevision(
                    revision_id=int(row["revision_id"]),
                    parent_revision_id=int(row["parent_revision_id"])
                    if row.get("parent_revision_id") is not None
                    else None,
                    page_id=int(row["page_id"]) if row.get("page_id") is not None else None,
                    title=str(row.get("title_at_retrieval") or "") or None,
                    availability_status="exact_response_unavailable_or_invalid",
                    api_sha1=str(row.get("sha1") or "") or None,
                    timestamp=str(row.get("timestamp") or "") or None,
                    actor_name_exact=str(row.get("actor_name_exact") or "") or None,
                    actor_user_id=int(row["actor_user_id"])
                    if row.get("actor_user_id") is not None
                    else None,
                    request_hash=str(row.get("request_hash") or "") or None,
                    response_content_sha256=str(row.get("response_content_sha256") or "") or None,
                    response_blob_path=str(row.get("response_blob_path") or "") or None,
                    response_json_pointer=str(row.get("response_json_pointer") or "") or None,
                    local_content_sha256=None,
                )
            existing = records.get(record.revision_id)
            if existing is None or (
                existing.availability_status != "content_available"
                and record.availability_status == "content_available"
            ):
                records[record.revision_id] = record
    return records


def _load_existing_method_b_index(path: Path) -> dict[int, CachedRevision]:
    if not path.exists():
        return {}
    output: dict[int, CachedRevision] = {}
    for row in pq.read_table(path).to_pylist():
        if row.get("revision_id") is None or "cache_version" not in row:
            continue
        record = CachedRevision(
            **{key: row.get(key) for key in CachedRevision.__dataclass_fields__}
        )
        output[record.revision_id] = record
    return output


def _network_fetch_needed(record: CachedRevision | None) -> bool:
    """Return whether explicit network mode may improve a cached observation."""

    if record is None:
        return True
    return record.availability_status not in {
        "content_available",
        "suppressed_or_revision_deleted_text",
        "deleted",
    }


def resolve_revision_text(settings: Settings, record: CachedRevision) -> RevisionText:
    state = record.availability_status
    if record.revision_id == 0:
        return RevisionText.available("0", "", api_sha1=None)
    availability = {
        "missing_page": RevisionAvailability.MISSING,
        "revision_not_returned": RevisionAvailability.MISSING,
        "suppressed_or_revision_deleted_text": RevisionAvailability.SUPPRESSED,
        "deleted": RevisionAvailability.DELETED,
    }.get(state, RevisionAvailability.UNAVAILABLE)
    if state != "content_available":
        return RevisionText(str(record.revision_id), availability, None, record.api_sha1)
    response = _read_exact_response(settings, record.to_row())
    found = _revision_from_response(response, record.revision_id)
    content = _content_from_revision(found[1]) if found else None
    if content is None:
        return RevisionText(
            str(record.revision_id), RevisionAvailability.UNAVAILABLE, None, record.api_sha1
        )
    computed = local_content_sha256(content)
    if record.local_content_sha256 not in (None, computed):
        raise RuntimeError(
            f"local content hash mismatch for cached revision {record.revision_id}"
        )
    return RevisionText(
        str(record.revision_id),
        RevisionAvailability.AVAILABLE,
        content,
        record.api_sha1,
        computed,
    )


def _fetch_revisions(
    settings: Settings,
    revision_ids: Iterable[int],
    *,
    batch_size: int,
) -> list[CachedRevision]:
    identifiers = sorted(set(revision_ids))
    if not identifiers:
        return []
    client = MediaWikiClient(settings)
    output: list[CachedRevision] = []
    for response, manifest in client.revisions_by_ids(
        identifiers, include_content=True, batch_size=min(50, batch_size)
    ):
        requested = [
            int(value)
            for value in str(manifest["normalized_parameters"].get("revids", "")).split("|")
            if value
        ]
        output.extend(
            cached_revision_from_api(response, manifest, revision_id)
            for revision_id in requested
        )
    return output


def hydrate_revision_pairs(
    settings: Settings,
    target_revision_ids: Iterable[int],
    *,
    index_path: Path,
    pairs_path: Path,
    history_path: Path,
    allow_network: bool = False,
    max_revisions: int | None = None,
    batch_size: int = 50,
    history_depth: int = 0,
) -> dict[str, Any]:
    """Resolve true parents cache-first; network is impossible without opt-in."""

    targets = sorted(set(target_revision_ids))
    if max_revisions is not None:
        targets = targets[:max_revisions]
    # Preserve every previously indexed Method-B observation, including pilot
    # controls, while resolving only currently required generic response blobs.
    records = _load_existing_method_b_index(index_path)
    records.update(
        load_cached_revision_index(settings, index_path, required_ids=set(targets))
    )
    missing_targets = [
        revision_id
        for revision_id in targets
        if _network_fetch_needed(records.get(revision_id))
    ]
    network_requests = 0
    if missing_targets and allow_network:
        fetched = _fetch_revisions(settings, missing_targets, batch_size=batch_size)
        network_requests += (len(missing_targets) + min(50, batch_size) - 1) // min(50, batch_size)
        records.update((record.revision_id, record) for record in fetched)

    parents = sorted(
        {
            record.parent_revision_id
            for revision_id in targets
            if (record := records.get(revision_id)) is not None
            and record.parent_revision_id not in (None, 0)
        }
    )
    records.update(
        load_cached_revision_index(settings, index_path, required_ids=set(parents))
    )
    missing_parents = [
        revision_id
        for revision_id in parents
        if _network_fetch_needed(records.get(revision_id))
    ]
    if missing_parents and allow_network:
        fetched = _fetch_revisions(settings, missing_parents, batch_size=batch_size)
        network_requests += (len(missing_parents) + min(50, batch_size) - 1) // min(50, batch_size)
        records.update((record.revision_id, record) for record in fetched)

    history_rows: list[dict[str, Any]] = []
    frontier = {
        target: records[target].parent_revision_id
        for target in targets
        if target in records and records[target].parent_revision_id not in (None, 0)
    }
    for depth in range(1, history_depth + 1):
        identifiers = sorted({revision_id for revision_id in frontier.values() if revision_id})
        records.update(
            load_cached_revision_index(settings, index_path, required_ids=set(identifiers))
        )
        missing = [
            revision_id
            for revision_id in identifiers
            if _network_fetch_needed(records.get(revision_id))
        ]
        if missing and allow_network:
            fetched = _fetch_revisions(settings, missing, batch_size=batch_size)
            network_requests += (len(missing) + min(50, batch_size) - 1) // min(50, batch_size)
            records.update((record.revision_id, record) for record in fetched)
        next_frontier: dict[int, int | None] = {}
        for target, revision_id in sorted(frontier.items()):
            record = records.get(revision_id or -1)
            history_rows.append(
                {
                    "target_revision_id": target,
                    "ancestor_revision_id": revision_id,
                    "history_depth": depth,
                    "availability_status": record.availability_status if record else "not_cached",
                    "local_content_sha256": record.local_content_sha256 if record else None,
                    "history_complete_through_depth": bool(record),
                    "cache_version": REVISION_CACHE_VERSION,
                }
            )
            if record and record.parent_revision_id not in (None, 0):
                next_frontier[target] = record.parent_revision_id
        frontier = next_frontier

    pairs: list[RevisionPair] = []
    for target_id in targets:
        target = records.get(target_id)
        parent_id = target.parent_revision_id if target else None
        parent = records.get(parent_id or -1)
        parent_state = "exact_empty_root" if parent_id == 0 else (
            parent.availability_status if parent else "not_cached"
        )
        pairs.append(
            RevisionPair(
                target_revision_id=target_id,
                predecessor_revision_id=parent_id,
                target_availability=target.availability_status if target else "not_cached",
                predecessor_availability=parent_state,
                parentid_verified=bool(target and parent_id is not None),
                page_id_consistent=(
                    target.page_id == parent.page_id if target and parent else None
                ),
            )
        )

    atomic_parquet(
        index_path,
        table_from_union_pylist(record.to_row() for _, record in sorted(records.items())),
    )
    atomic_parquet(pairs_path, table_from_union_pylist(pair.to_row() for pair in pairs))
    atomic_parquet(history_path, table_from_union_pylist(history_rows))
    report = {
        "cache_version": REVISION_CACHE_VERSION,
        "target_revision_count": len(targets),
        "target_not_cached": sum(pair.target_availability == "not_cached" for pair in pairs),
        "predecessor_not_cached": sum(
            pair.predecessor_availability == "not_cached" for pair in pairs
        ),
        "target_content_unavailable": sum(
            pair.target_availability != "content_available" for pair in pairs
        ),
        "predecessor_content_unavailable": sum(
            pair.predecessor_availability not in {"content_available", "exact_empty_root"}
            for pair in pairs
        ),
        "history_depth": history_depth,
        "history_rows": len(history_rows),
        "network_allowed": allow_network,
        "network_request_batches": network_requests,
        "completeness_status": (
            "bounded" if max_revisions is not None else "full_population_profiled"
        ),
    }
    atomic_write_json(pairs_path.with_suffix(".json"), report)
    return report
