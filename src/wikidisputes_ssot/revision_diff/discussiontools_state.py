"""Durable, hash-bound state for rendered DiscussionTools recovery runs.

The database is deliberately only an execution cache: exact raw wikitext and
the stored parser evidence remain the inputs to any later safety decision.
Each completed revision is committed independently (or in caller-selected
batches), so an interrupted render run can safely resume without treating a
partially written result as complete.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wikidisputes_ssot.hashing import (
    canonical_json_bytes,
    canonical_json_hash,
    sha256_bytes,
)

DISCUSSIONTOOLS_STATE_SCHEMA_VERSION = "discussiontools-rendered-state-v2"
COMPLETED_STATUSES = frozenset({"success", "failed", "unavailable"})


class DiscussionToolsStateError(RuntimeError):
    """Base class for durable rendered-structure state errors."""


class StateManifestMismatch(DiscussionToolsStateError):
    """The requested run does not match the database's immutable manifest."""


class RevisionInputMismatch(DiscussionToolsStateError):
    """A revision ID was supplied with different archival raw wikitext."""


@dataclass(frozen=True, slots=True)
class DiscussionToolsStateManifest:
    """Hash binding for one reproducible feasibility or production run."""

    config_sha256: str
    code_sha256: str
    input_sha256: str
    schema_version: str = DISCUSSIONTOOLS_STATE_SCHEMA_VERSION

    @classmethod
    def from_values(
        cls,
        *,
        config: Mapping[str, Any],
        code: Mapping[str, Any] | str,
        inputs: Mapping[str, Any],
    ) -> DiscussionToolsStateManifest:
        """Create bindings from canonical, JSON-serializable inputs."""

        return cls(
            config_sha256=canonical_json_hash(config),
            code_sha256=canonical_json_hash(code),
            input_sha256=canonical_json_hash(inputs),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "config_sha256": self.config_sha256,
            "code_sha256": self.code_sha256,
            "input_sha256": self.input_sha256,
        }


@dataclass(frozen=True, slots=True)
class DiscussionToolsRevisionState:
    """One terminal per-revision parser/render result."""

    revision_id: int
    status: str
    raw_wikitext_sha256: str | None
    content_hashes: dict[str, str]
    content_hashes_sha256: str
    payload: dict[str, Any]
    payload_sha256: str
    error: dict[str, Any] | None
    error_sha256: str | None
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True, slots=True)
class DiscussionToolsRenderResource:
    """One independently durable fetched render resource for a revision."""

    revision_id: int
    resource_kind: str
    status: str
    raw_wikitext_sha256: str | None
    source_url: str
    content_sha256: str | None
    blob_path: str | None
    http_metadata: dict[str, Any]
    http_metadata_sha256: str
    error: dict[str, Any] | None
    error_sha256: str | None
    created_at_utc: str
    updated_at_utc: str


def _canonical_json_text(value: Mapping[str, Any]) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _validate_sha256(value: str | None, name: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _validated_content_hashes(content_hashes: Mapping[str, str] | None) -> dict[str, str]:
    output = dict(content_hashes or {})
    for name, digest in output.items():
        if not isinstance(name, str) or not name:
            raise ValueError("content hash names must be non-empty strings")
        if not isinstance(digest, str):
            raise ValueError(f"content hash {name!r} must be a string")
        _validate_sha256(digest, f"content hash {name!r}")
    return output


def _json_object_with_hash(value: Mapping[str, Any]) -> tuple[str, str]:
    encoded = _canonical_json_text(value)
    return encoded, sha256_bytes(encoded.encode("utf-8"))


def _read_checked_json_object(
    encoded: str,
    expected_hash: str,
    *,
    label: str,
) -> dict[str, Any]:
    if sha256_bytes(encoded.encode("utf-8")) != expected_hash:
        raise DiscussionToolsStateError(f"stored {label} hash mismatch")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise DiscussionToolsStateError(f"stored {label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise DiscussionToolsStateError(f"stored {label} JSON must be an object")
    return value


class DiscussionToolsStateStore:
    """SQLite execution state with manifest and per-revision input binding.

    ``record`` commits a single result; ``transaction`` lets a caller commit a
    25--50 revision checkpoint atomically.  A SIGINT or process shutdown while
    a transaction is active rolls the entire active transaction back.
    """

    def __init__(
        self,
        path: Path,
        manifest: DiscussionToolsStateManifest,
        *,
        resume: bool = True,
    ) -> None:
        self.path = path
        self.manifest = manifest
        self.resume = resume
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, isolation_level=None)
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self._create_schema()
            self._bind_manifest()
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> DiscussionToolsStateStore:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_manifest (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    manifest_json TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS revision_state (
                    revision_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'unavailable')),
                    raw_wikitext_sha256 TEXT,
                    content_hashes_json TEXT NOT NULL,
                    content_hashes_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    error_json TEXT,
                    error_sha256 TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS render_resource (
                    revision_id INTEGER NOT NULL,
                    resource_kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'unavailable')),
                    raw_wikitext_sha256 TEXT,
                    source_url TEXT NOT NULL,
                    content_sha256 TEXT,
                    blob_path TEXT,
                    http_metadata_json TEXT NOT NULL,
                    http_metadata_sha256 TEXT NOT NULL,
                    error_json TEXT,
                    error_sha256 TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (revision_id, resource_kind)
                )
                """
            )

    def _bind_manifest(self) -> None:
        manifest_json = _canonical_json_text(self.manifest.to_dict())
        manifest_sha256 = sha256_bytes(manifest_json.encode("utf-8"))
        row = self.connection.execute(
            "SELECT manifest_json, manifest_sha256 FROM run_manifest WHERE singleton = 1"
        ).fetchone()
        if row is None:
            with self.transaction():
                self.connection.execute(
                    "INSERT INTO run_manifest "
                    "(singleton, manifest_json, manifest_sha256) VALUES (1, ?, ?)",
                    (manifest_json, manifest_sha256),
                )
            return
        if row["manifest_sha256"] != sha256_bytes(str(row["manifest_json"]).encode("utf-8")):
            raise StateManifestMismatch("stored DiscussionTools state manifest is corrupt")
        if row["manifest_json"] != manifest_json:
            raise StateManifestMismatch(
                "DiscussionTools state manifest differs (config, code, or input hash mismatch)"
            )

    @contextmanager
    def transaction(self) -> Iterator[DiscussionToolsStateStore]:
        """Commit all enclosed writes, or roll them back on every exception."""

        if self.connection.in_transaction:
            yield self
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def get(self, revision_id: int) -> DiscussionToolsRevisionState | None:
        row = self.connection.execute(
            "SELECT * FROM revision_state WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        return self._state_from_row(row) if row is not None else None

    def should_process(self, revision_id: int) -> bool:
        """Honor ``--resume`` by skipping every terminal stored revision."""

        return not self.resume or self.get(revision_id) is None

    def pending_revision_ids(self, revision_ids: Iterable[int]) -> list[int]:
        """Return the supplied IDs whose terminal state must still be produced."""

        identifiers = list(dict.fromkeys(revision_ids))
        if not self.resume:
            return identifiers
        return [revision_id for revision_id in identifiers if self.should_process(revision_id)]

    def get_render_resource(
        self, revision_id: int, resource_kind: str
    ) -> DiscussionToolsRenderResource | None:
        row = self.connection.execute(
            "SELECT * FROM render_resource WHERE revision_id = ? AND resource_kind = ?",
            (revision_id, resource_kind),
        ).fetchone()
        return self._resource_from_row(row) if row is not None else None

    def should_hydrate_render_resource(self, revision_id: int, resource_kind: str) -> bool:
        """Honor ``--resume`` without coupling hydration to parser completion."""

        return not self.resume or self.get_render_resource(revision_id, resource_kind) is None

    def pending_render_resources(
        self, resource_keys: Iterable[tuple[int, str]]
    ) -> list[tuple[int, str]]:
        """Return resource keys whose terminal hydration state is absent."""

        keys = list(dict.fromkeys(resource_keys))
        if not self.resume:
            return keys
        return [
            (revision_id, resource_kind)
            for revision_id, resource_kind in keys
            if self.should_hydrate_render_resource(revision_id, resource_kind)
        ]

    def record(
        self,
        *,
        revision_id: int,
        status: str,
        raw_wikitext_sha256: str | None,
        content_hashes: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        overwrite: bool = False,
    ) -> DiscussionToolsRevisionState:
        """Atomically record an explicit terminal state for one revision."""

        if status not in COMPLETED_STATUSES:
            raise ValueError(f"unsupported terminal state: {status!r}")
        _validate_sha256(raw_wikitext_sha256, "raw_wikitext_sha256")
        hashes = _validated_content_hashes(content_hashes)
        payload_value = dict(payload or {})
        error_value = dict(error) if error is not None else None
        hashes_json, hashes_sha256 = _json_object_with_hash(hashes)
        payload_json, payload_sha256 = _json_object_with_hash(payload_value)
        error_json: str | None = None
        error_sha256: str | None = None
        if error_value is not None:
            error_json, error_sha256 = _json_object_with_hash(error_value)
        with self.transaction():
            existing = self.get(revision_id)
            if existing is not None:
                if existing.raw_wikitext_sha256 != raw_wikitext_sha256:
                    raise RevisionInputMismatch(
                        f"revision {revision_id} has a different raw wikitext hash"
                    )
                if not overwrite:
                    return existing
            self.connection.execute(
                """
                INSERT INTO revision_state (
                    revision_id, status, raw_wikitext_sha256, content_hashes_json,
                    content_hashes_sha256, payload_json, payload_sha256, error_json,
                    error_sha256, created_at_utc, updated_at_utc
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                ON CONFLICT(revision_id) DO UPDATE SET
                    status = excluded.status,
                    content_hashes_json = excluded.content_hashes_json,
                    content_hashes_sha256 = excluded.content_hashes_sha256,
                    payload_json = excluded.payload_json,
                    payload_sha256 = excluded.payload_sha256,
                    error_json = excluded.error_json,
                    error_sha256 = excluded.error_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    revision_id,
                    status,
                    raw_wikitext_sha256,
                    hashes_json,
                    hashes_sha256,
                    payload_json,
                    payload_sha256,
                    error_json,
                    error_sha256,
                ),
            )
        recorded = self.get(revision_id)
        assert recorded is not None
        return recorded

    def record_render_resource(
        self,
        *,
        revision_id: int,
        resource_kind: str,
        status: str,
        raw_wikitext_sha256: str | None,
        source_url: str,
        content_sha256: str | None = None,
        blob_path: str | None = None,
        http_metadata: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        overwrite: bool = False,
    ) -> DiscussionToolsRenderResource:
        """Immediately persist fetched HTML/source evidence before parser work.

        Successful resources must point to immutable content by its digest and
        cache/blob path.  Failed and unavailable attempts retain their exact URL
        and HTTP/error evidence, rather than disappearing on resume.
        """

        if status not in COMPLETED_STATUSES:
            raise ValueError(f"unsupported terminal state: {status!r}")
        if not resource_kind:
            raise ValueError("resource_kind must be a non-empty string")
        if not source_url:
            raise ValueError("source_url must be a non-empty exact URL")
        _validate_sha256(raw_wikitext_sha256, "raw_wikitext_sha256")
        _validate_sha256(content_sha256, "content_sha256")
        if (content_sha256 is None) != (not blob_path):
            raise ValueError("content_sha256 and blob_path must be recorded together")
        if status == "success" and (content_sha256 is None or not blob_path):
            raise ValueError("successful render resources require content_sha256 and blob_path")
        metadata_json, metadata_sha256 = _json_object_with_hash(dict(http_metadata or {}))
        error_json: str | None = None
        error_sha256: str | None = None
        if error is not None:
            error_json, error_sha256 = _json_object_with_hash(dict(error))
        with self.transaction():
            existing = self.get_render_resource(revision_id, resource_kind)
            if existing is not None:
                if existing.raw_wikitext_sha256 != raw_wikitext_sha256:
                    raise RevisionInputMismatch(
                        f"revision {revision_id} has a different raw wikitext hash"
                    )
                if not overwrite:
                    return existing
            self.connection.execute(
                """
                INSERT INTO render_resource (
                    revision_id, resource_kind, status, raw_wikitext_sha256, source_url,
                    content_sha256, blob_path, http_metadata_json, http_metadata_sha256,
                    error_json, error_sha256, created_at_utc, updated_at_utc
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                ON CONFLICT(revision_id, resource_kind) DO UPDATE SET
                    status = excluded.status,
                    source_url = excluded.source_url,
                    content_sha256 = excluded.content_sha256,
                    blob_path = excluded.blob_path,
                    http_metadata_json = excluded.http_metadata_json,
                    http_metadata_sha256 = excluded.http_metadata_sha256,
                    error_json = excluded.error_json,
                    error_sha256 = excluded.error_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    revision_id,
                    resource_kind,
                    status,
                    raw_wikitext_sha256,
                    source_url,
                    content_sha256,
                    blob_path,
                    metadata_json,
                    metadata_sha256,
                    error_json,
                    error_sha256,
                ),
            )
        recorded = self.get_render_resource(revision_id, resource_kind)
        assert recorded is not None
        return recorded

    def _state_from_row(self, row: sqlite3.Row) -> DiscussionToolsRevisionState:
        payload_json = str(row["payload_json"])
        content_hashes = _read_checked_json_object(
            str(row["content_hashes_json"]),
            str(row["content_hashes_sha256"]),
            label="content hashes",
        )
        payload = _read_checked_json_object(
            payload_json,
            str(row["payload_sha256"]),
            label="payload",
        )
        error: dict[str, Any] | None = None
        if row["error_json"] is not None:
            if row["error_sha256"] is None:
                raise DiscussionToolsStateError("stored error JSON is missing its hash")
            error = _read_checked_json_object(
                str(row["error_json"]), str(row["error_sha256"]), label="revision error"
            )
        elif row["error_sha256"] is not None:
            raise DiscussionToolsStateError("stored error hash has no error JSON")
        _validate_sha256(row["raw_wikitext_sha256"], "raw_wikitext_sha256")
        validated_hashes = _validated_content_hashes(content_hashes)
        return DiscussionToolsRevisionState(
            revision_id=int(row["revision_id"]),
            status=str(row["status"]),
            raw_wikitext_sha256=row["raw_wikitext_sha256"],
            content_hashes=validated_hashes,
            content_hashes_sha256=str(row["content_hashes_sha256"]),
            payload=payload,
            payload_sha256=str(row["payload_sha256"]),
            error=error,
            error_sha256=row["error_sha256"],
            created_at_utc=str(row["created_at_utc"]),
            updated_at_utc=str(row["updated_at_utc"]),
        )

    def _resource_from_row(self, row: sqlite3.Row) -> DiscussionToolsRenderResource:
        content_sha256 = row["content_sha256"]
        _validate_sha256(row["raw_wikitext_sha256"], "raw_wikitext_sha256")
        _validate_sha256(content_sha256, "content_sha256")
        metadata = _read_checked_json_object(
            str(row["http_metadata_json"]),
            str(row["http_metadata_sha256"]),
            label="HTTP metadata",
        )
        error: dict[str, Any] | None = None
        if row["error_json"] is not None:
            if row["error_sha256"] is None:
                raise DiscussionToolsStateError("stored error JSON is missing its hash")
            error = _read_checked_json_object(
                str(row["error_json"]), str(row["error_sha256"]), label="error"
            )
        elif row["error_sha256"] is not None:
            raise DiscussionToolsStateError("stored error hash has no error JSON")
        return DiscussionToolsRenderResource(
            revision_id=int(row["revision_id"]),
            resource_kind=str(row["resource_kind"]),
            status=str(row["status"]),
            raw_wikitext_sha256=row["raw_wikitext_sha256"],
            source_url=str(row["source_url"]),
            content_sha256=content_sha256,
            blob_path=row["blob_path"],
            http_metadata=metadata,
            http_metadata_sha256=str(row["http_metadata_sha256"]),
            error=error,
            error_sha256=row["error_sha256"],
            created_at_utc=str(row["created_at_utc"]),
            updated_at_utc=str(row["updated_at_utc"]),
        )
