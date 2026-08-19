from __future__ import annotations

import datetime as dt
import gzip
import json
import random
import time
from collections.abc import Iterable, Iterator
from typing import Any

import httpx

from .config import Settings
from .hashing import canonical_json_hash
from .io import BlobStore, atomic_write_json


class MediaWikiClient:
    """Polite, exact-response, resumable MediaWiki Action API client."""

    def __init__(self, settings: Settings) -> None:
        if not settings.network.contact_is_configured:
            raise ValueError(
                "MediaWiki hydration requires a configured identifying User-Agent contact URL/email"
            )
        self.settings = settings
        self.blobs = BlobStore(settings.roots.data / "bronze" / "blobs")
        self.manifests = settings.roots.data / "bronze" / "mediawiki" / "requests"
        self.manifests.mkdir(parents=True, exist_ok=True)
        self._last_request_monotonic = 0.0
        self._http = httpx.Client(
            timeout=self.settings.network.timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.network.user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )

    def _pace(self) -> None:
        interval = 1.0 / self.settings.network.requests_per_second
        remaining = interval - (time.monotonic() - self._last_request_monotonic)
        if remaining > 0:
            time.sleep(remaining)

    def request(self, parameters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        params = {
            **parameters,
            "format": "json",
            "formatversion": "2",
            "maxlag": str(self.settings.network.maxlag),
        }
        normalized = {str(key): str(value) for key, value in sorted(params.items())}
        request_hash = canonical_json_hash(
            {"endpoint": self.settings.mediawiki.endpoint, "parameters": normalized}
        )
        request_root = self.manifests / request_hash
        success = request_root / "SUCCESS.json"
        if success.exists():
            manifest = json.loads(success.read_text(encoding="utf-8"))
            blob = self.settings.roots.data / "bronze" / "blobs" / manifest["blob_path"]
            stored = blob.read_bytes()
            body = gzip.decompress(stored) if manifest.get("storage_encoding") else stored
            return json.loads(body), manifest
        request_root.mkdir(parents=True, exist_ok=True)
        attempts = self.settings.network.max_attempts
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._pace()
            retrieved_at = dt.datetime.now(dt.UTC).isoformat()
            try:
                response = self._http.get(self.settings.mediawiki.endpoint, params=normalized)
                self._last_request_monotonic = time.monotonic()
                body = response.content
                body_blob = self.blobs.put_gzip(body, suffix=".json.gz")
                base_manifest = {
                    "endpoint": self.settings.mediawiki.endpoint,
                    "normalized_parameters": normalized,
                    "request_hash": request_hash,
                    "retrieved_at_utc": retrieved_at,
                    "http_status": response.status_code,
                    "response_headers": {
                        key.lower(): value
                        for key, value in response.headers.items()
                        if key.lower()
                        in {
                            "content-type",
                            "content-encoding",
                            "retry-after",
                            "date",
                            "etag",
                            "last-modified",
                            "x-database-lag",
                        }
                    },
                    **body_blob,
                    "attempt": attempt,
                }
                atomic_write_json(request_root / f"attempt-{attempt:02d}.json", base_manifest)
                retry_after = response.headers.get("retry-after")
                if response.status_code in {429, 503}:
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else None
                    raise RetryableAPIError("HTTP rate-limit/maxlag", delay)
                response.raise_for_status()
                parsed = response.json()
                api_error = parsed.get("error") if isinstance(parsed, dict) else None
                if isinstance(api_error, dict):
                    code = api_error.get("code")
                    if code in {"maxlag", "ratelimited", "readonly"}:
                        lag = api_error.get("lag")
                        raise RetryableAPIError(f"MediaWiki API {code}; lag={lag}", None)
                    failure = {**base_manifest, "api_status": "error", "api_error": api_error}
                    atomic_write_json(request_root / "FAILURE.json", failure)
                    raise RuntimeError(f"MediaWiki API error {code}: {api_error.get('info')}")
                manifest = {**base_manifest, "api_status": "success"}
                atomic_write_json(success, manifest)
                return parsed, manifest
            except RetryableAPIError as exc:
                last_error = exc
                if attempt == attempts:
                    break
                delay = exc.retry_after or min(60.0, 2.0**attempt)
                delay += random.Random(int(request_hash[:8], 16) + attempt).random()
                time.sleep(delay)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == attempts:
                    break
                delay = min(60.0, 2.0**attempt)
                delay += random.Random(int(request_hash[:8], 16) + attempt).random()
                time.sleep(delay)
        failure = {
            "endpoint": self.settings.mediawiki.endpoint,
            "normalized_parameters": normalized,
            "request_hash": request_hash,
            "api_status": "retrieval_failed",
            "attempts": attempts,
            "error_type": type(last_error).__name__ if last_error else "unknown",
            "error": str(last_error) if last_error else "unknown",
        }
        atomic_write_json(request_root / "FAILURE.json", failure)
        raise RuntimeError(f"MediaWiki request failed: {request_hash}") from last_error

    def revisions_by_ids(
        self, revision_ids: Iterable[int], *, include_content: bool = True, batch_size: int = 50
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        properties = "ids|flags|timestamp|user|userid|size|sha1|contentmodel|comment|tags|roles"
        if include_content:
            properties += "|content"
        batch: list[int] = []
        for revision_id in revision_ids:
            batch.append(revision_id)
            if len(batch) >= batch_size:
                yield self.request(
                    {
                        "action": "query",
                        "prop": "revisions",
                        "revids": "|".join(map(str, batch)),
                        "rvslots": "main",
                        "rvprop": properties,
                    }
                )
                batch.clear()
        if batch:
            yield self.request(
                {
                    "action": "query",
                    "prop": "revisions",
                    "revids": "|".join(map(str, batch)),
                    "rvslots": "main",
                    "rvprop": properties,
                }
            )

    def full_page_history(
        self, page_id: int, *, start: str | None = None, end: str | None = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        continuation: dict[str, str] = {}
        while True:
            parameters: dict[str, Any] = {
                "action": "query",
                "prop": "revisions",
                "pageids": page_id,
                "rvslots": "main",
                "rvprop": (
                    "ids|flags|timestamp|user|userid|size|sha1|contentmodel|comment|tags|roles"
                ),
                "rvlimit": "max",
                "rvdir": "newer",
                **continuation,
            }
            if start:
                parameters["rvstart"] = start
            if end:
                parameters["rvend"] = end
            parsed, manifest = self.request(parameters)
            yield parsed, manifest
            next_values = parsed.get("continue") if isinstance(parsed, dict) else None
            if not isinstance(next_values, dict):
                return
            continuation = {str(key): str(value) for key, value in next_values.items()}

    def revision_at_or_before(
        self, page_id: int, timestamp: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Retrieve one baseline revision at/before an observation window."""
        return self.request(
            {
                "action": "query",
                "prop": "revisions",
                "pageids": page_id,
                "rvslots": "main",
                "rvprop": (
                    "ids|flags|timestamp|user|userid|size|sha1|contentmodel|comment|tags|roles"
                ),
                "rvlimit": "1",
                "rvdir": "older",
                "rvstart": timestamp,
            }
        )

    def parse_revision(self, revision_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.request(
            {
                "action": "parse",
                "oldid": revision_id,
                "prop": "text|wikitext|links|externallinks|templates|tocdata|revid|parsewarnings",
                "disablelimitreport": "1",
                "disableeditsection": "1",
                "useskin": "vector",
            }
        )

    def compare(
        self, from_revision: int, to_revision: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.request(
            {
                "action": "compare",
                "fromrev": from_revision,
                "torev": to_revision,
                "prop": "ids|title|user|comment|parsedcomment|size|rel|timestamp|diff|diffsize",
            }
        )


class RetryableAPIError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def revision_availability(page: dict[str, Any], revision: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize MediaWiki absence and revision-deletion states without collapsing them."""
    if page.get("missing") is True or "missing" in page:
        state = "missing_page"
    elif revision is None:
        state = "revision_not_returned"
    elif "texthidden" in revision:
        state = "suppressed_or_revision_deleted_text"
    elif "slots" not in revision and "content" not in revision:
        state = "metadata_only"
    else:
        state = "content_available"
    revision = revision or {}
    return {
        "availability_status": state,
        "userhidden": "userhidden" in revision,
        "sha1hidden": "sha1hidden" in revision,
        "commenthidden": "commenthidden" in revision,
        "texthidden": "texthidden" in revision,
        "page_missing": page.get("missing") is True or "missing" in page,
        "revision_missing": revision.get("missing") is True or "missing" in revision,
    }
