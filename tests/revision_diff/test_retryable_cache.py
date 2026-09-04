import gzip
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from wikidisputes_ssot.config import Settings
from wikidisputes_ssot.revision_diff.cache import (
    CachedRevision,
    _network_fetch_needed,
    _validated_fetched_revision,
    load_cached_revision_index,
    revision_acquisition_class,
    revision_acquisition_provenance,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "schema_version": "test",
            "identity_algorithm_version": "test",
            "representation_version": "test",
            "join_contract_version": "test",
            "dv_definition_version": "test",
            "canonical_serialization_version": "test",
            "roots": {
                "data": str(tmp_path / "data"),
                "cache": str(tmp_path / "cache"),
                "output": str(tmp_path / "output"),
                "checkpoints": str(tmp_path / "checkpoints"),
            },
            "network": {"user_agent": "WikiDisputesSSOT test", "contact_is_configured": False},
            "wikiconv": {"years": [2020], "base_url": "https://example.test"},
            "mediawiki": {"endpoint": "https://example.test/w/api.php"},
            "run": {},
        }
    )


def _response(revision_id: int = 12, *, parentid: int = 0, text: str = "source") -> dict:
    return {
        "query": {
            "pages": [
                {
                    "pageid": 1,
                    "title": "Talk:Example",
                    "revisions": [
                        {
                            "revid": revision_id,
                            "parentid": parentid,
                            "sha1": "api-sha",
                            "slots": {"main": {"content": text}},
                        }
                    ],
                }
            ]
        }
    }


def _manifest(settings: Settings, response: dict, *, corrupt_hash: bool = False) -> dict:
    body = json.dumps(response).encode()
    relative = "retry-test.json.gz"
    path = settings.roots.data / "bronze" / "blobs" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(body))
    return {
        "request_hash": "request",
        "content_sha256": "bad" if corrupt_hash else hashlib.sha256(body).hexdigest(),
        "blob_path": relative,
    }


def test_acquisition_class_retries_only_retryable_states() -> None:
    retryable = CachedRevision(
        1,
        None,
        None,
        None,
        "transient_cache_error",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    unavailable = CachedRevision(
        2,
        None,
        None,
        None,
        "revision_not_returned",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    assert revision_acquisition_class(None) == "retryable"
    assert _network_fetch_needed(retryable) is True
    assert _network_fetch_needed(unavailable) is False
    assert revision_acquisition_provenance(unavailable) == {
        "acquisition_class": "true_unavailable",
        "availability_status": "revision_not_returned",
        "network_retryable": False,
    }
    unknown = CachedRevision(
        3, None, None, None, "legacy_unknown", None, None, None, None, None, None, None, None, None
    )
    assert revision_acquisition_class(unknown) == "unknown"
    assert _network_fetch_needed(unknown) is False


def test_fetched_record_requires_exact_persisted_id_blob_hash_and_parentid(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    response = _response()
    valid = _validated_fetched_revision(settings, response, _manifest(settings, response), 12)
    assert valid.availability_status == "content_available"
    assert valid.parent_revision_id == 0
    assert revision_acquisition_class(valid) == "available"
    # The same exact response remains safe and classifies identically on resume.
    assert (
        _validated_fetched_revision(settings, response, _manifest(settings, response), 12) == valid
    )

    invalid_hash = _validated_fetched_revision(
        settings, response, _manifest(settings, response, corrupt_hash=True), 12
    )
    assert invalid_hash.availability_status == "exact_response_unavailable_or_invalid"
    assert _network_fetch_needed(invalid_hash) is True

    missing_parent = _response()
    del missing_parent["query"]["pages"][0]["revisions"][0]["parentid"]
    invalid_parent = _validated_fetched_revision(
        settings, missing_parent, _manifest(settings, missing_parent), 12
    )
    assert invalid_parent.availability_status == "exact_response_unavailable_or_invalid"


def test_cached_content_is_reused_only_after_blob_and_local_hash_validation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    response = _response()
    manifest = _manifest(settings, response)
    valid = _validated_fetched_revision(settings, response, manifest, 12)
    index = tmp_path / "index.parquet"
    bad = {**valid.to_row(), "local_content_sha256": None}
    pq.write_table(pa.Table.from_pylist([bad]), index)

    loaded = load_cached_revision_index(settings, index, required_ids={12})
    assert loaded[12].availability_status == "exact_response_unavailable_or_invalid"
    assert revision_acquisition_class(loaded[12]) == "retryable"


def test_true_unavailable_response_still_requires_valid_persisted_blob(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    response = {"query": {"pages": []}}
    record = _validated_fetched_revision(
        settings, response, _manifest(settings, response, corrupt_hash=True), 12
    )
    assert record.availability_status == "exact_response_unavailable_or_invalid"
