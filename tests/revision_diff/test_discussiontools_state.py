from __future__ import annotations

import sqlite3

import pytest

from wikidisputes_ssot.hashing import sha256_bytes
from wikidisputes_ssot.revision_diff.discussiontools_state import (
    DiscussionToolsStateManifest,
    DiscussionToolsStateStore,
    RevisionInputMismatch,
    StateManifestMismatch,
)


def _manifest(*, sample: str = "sample-a") -> DiscussionToolsStateManifest:
    return DiscussionToolsStateManifest.from_values(
        config={"parser": "DiscussionTools", "checkpoint_every": 25},
        code={"discussiontools_commit": "abc123"},
        inputs={"sample": sample},
    )


def _raw(text: str = "raw source") -> str:
    return sha256_bytes(text.encode("utf-8"))


def test_records_hashes_payload_and_terminal_resume_skip(tmp_path):
    path = tmp_path / "discussiontools.sqlite"
    with DiscussionToolsStateStore(path, _manifest(), resume=True) as store:
        state = store.record(
            revision_id=101,
            status="success",
            raw_wikitext_sha256=_raw(),
            content_hashes={"rendered_html": _raw("html"), "parser_output": _raw("parsed")},
            payload={"html_source": "Parsoid", "comments": [{"author": "Example"}]},
        )

        assert state.status == "success"
        assert state.payload["html_source"] == "Parsoid"
        assert state.content_hashes["rendered_html"] == _raw("html")
        assert state.content_hashes_sha256 == _raw(
            '{"parser_output":"' + _raw("parsed") + '","rendered_html":"' + _raw("html") + '"}'
        )
        assert store.should_process(101) is False
        assert store.pending_revision_ids([101, 102, 102]) == [102]

        # A completed row is immutable by default, so resume cannot accidentally
        # replace exact parser evidence with a later run's observation.
        unchanged = store.record(
            revision_id=101,
            status="failed",
            raw_wikitext_sha256=_raw(),
            payload={"unexpected": True},
        )
        assert unchanged.status == "success"
        assert unchanged.payload == state.payload

    with DiscussionToolsStateStore(path, _manifest(), resume=False) as store:
        assert store.should_process(101) is True
        assert store.pending_revision_ids([101, 102]) == [101, 102]


def test_manifest_and_per_revision_raw_hash_mismatches_fail_closed(tmp_path):
    path = tmp_path / "discussiontools.sqlite"
    with DiscussionToolsStateStore(path, _manifest()) as store:
        store.record(
            revision_id=101,
            status="unavailable",
            raw_wikitext_sha256=_raw(),
            error={"reason": "revision_deleted"},
        )
        with pytest.raises(RevisionInputMismatch, match="different raw wikitext hash"):
            store.record(
                revision_id=101,
                status="success",
                raw_wikitext_sha256=_raw("different raw"),
            )

    with pytest.raises(StateManifestMismatch, match="config, code, or input hash mismatch"):
        DiscussionToolsStateStore(path, _manifest(sample="sample-b"))


def test_transaction_rolls_back_interrupted_checkpoint_and_persists_prior_one(tmp_path):
    path = tmp_path / "discussiontools.sqlite"
    with DiscussionToolsStateStore(path, _manifest()) as store:
        # This models a caller checkpointing 25--50 revisions in one durable
        # transaction, then receiving SIGINT before its next checkpoint commits.
        with store.transaction():
            store.record(revision_id=1, status="success", raw_wikitext_sha256=_raw("one"))
            store.record(revision_id=2, status="failed", raw_wikitext_sha256=_raw("two"))
        with pytest.raises(KeyboardInterrupt), store.transaction():
            store.record(revision_id=3, status="success", raw_wikitext_sha256=_raw("three"))
            raise KeyboardInterrupt

        assert store.get(1) is not None
        assert store.get(2) is not None
        assert store.get(3) is None

    # A fresh process sees only the committed checkpoint.
    with DiscussionToolsStateStore(path, _manifest()) as resumed:
        assert resumed.pending_revision_ids([1, 2, 3]) == [3]


def test_detects_payload_corruption_when_reading_state(tmp_path):
    path = tmp_path / "discussiontools.sqlite"
    with DiscussionToolsStateStore(path, _manifest()) as store:
        store.record(revision_id=1, status="success", raw_wikitext_sha256=_raw())

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE revision_state SET payload_json = ? WHERE revision_id = 1",
        ('{"bad":true}',),
    )
    connection.commit()
    connection.close()

    with (
        DiscussionToolsStateStore(path, _manifest()) as store,
        pytest.raises(RuntimeError, match="payload hash mismatch"),
    ):
        store.get(1)


def test_detects_revision_content_hash_and_error_corruption(tmp_path):
    path = tmp_path / "discussiontools.sqlite"
    with DiscussionToolsStateStore(path, _manifest()) as store:
        store.record(
            revision_id=1,
            status="failed",
            raw_wikitext_sha256=_raw(),
            content_hashes={"html": _raw("html")},
            error={"reason": "parser_failed"},
        )

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE revision_state SET content_hashes_json = ? WHERE revision_id = 1",
        ('{"html":"' + _raw("different") + '"}',),
    )
    connection.commit()
    connection.close()
    with (
        DiscussionToolsStateStore(path, _manifest()) as store,
        pytest.raises(RuntimeError, match="content hashes hash mismatch"),
    ):
        store.get(1)

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE revision_state SET content_hashes_json = ?, error_json = ? WHERE revision_id = 1",
        ('{"html":"' + _raw("html") + '"}', '{"reason":"changed"}'),
    )
    connection.commit()
    connection.close()
    with (
        DiscussionToolsStateStore(path, _manifest()) as store,
        pytest.raises(RuntimeError, match="revision error hash mismatch"),
    ):
        store.get(1)


def test_render_resources_are_independent_durable_hydration_checkpoints(tmp_path):
    path = tmp_path / "discussiontools.sqlite"
    with DiscussionToolsStateStore(path, _manifest(), resume=True) as store:
        html = store.record_render_resource(
            revision_id=501,
            resource_kind="parsoid_html",
            status="success",
            raw_wikitext_sha256=_raw("revision 501"),
            source_url="https://example.test/w/rest.php/v1/revision/501/html",
            content_sha256=_raw("<p>rendered</p>"),
            blob_path="sha256/ab/rendered.html.gz",
            http_metadata={"status": 200, "etag": "exact-etag"},
        )
        unavailable = store.record_render_resource(
            revision_id=502,
            resource_kind="parsoid_html",
            status="unavailable",
            raw_wikitext_sha256=_raw("revision 502"),
            source_url="https://example.test/w/rest.php/v1/revision/502/html",
            http_metadata={"status": 404},
            error={"reason": "revision_not_renderable"},
        )
        assert html.content_sha256 == _raw("<p>rendered</p>")
        assert unavailable.error == {"reason": "revision_not_renderable"}
        assert store.should_hydrate_render_resource(501, "parsoid_html") is False
        assert store.pending_render_resources(
            [(501, "parsoid_html"), (502, "parsoid_html"), (503, "parsoid_html")]
        ) == [(503, "parsoid_html")]

        # Parser/mapping interruption after an immediately committed hydration
        # result cannot discard the fetched HTML cache entry.
        with pytest.raises(KeyboardInterrupt), store.transaction():
            store.record(
                revision_id=501,
                status="success",
                raw_wikitext_sha256=_raw("revision 501"),
            )
            raise KeyboardInterrupt
        assert store.get(501) is None
        assert store.get_render_resource(501, "parsoid_html") == html

    with DiscussionToolsStateStore(path, _manifest(), resume=False) as store:
        assert store.should_hydrate_render_resource(501, "parsoid_html") is True


def test_render_resource_enforces_content_evidence_and_detects_metadata_corruption(tmp_path):
    path = tmp_path / "discussiontools.sqlite"
    with DiscussionToolsStateStore(path, _manifest()) as store:
        with pytest.raises(ValueError, match="content_sha256 and blob_path"):
            store.record_render_resource(
                revision_id=701,
                resource_kind="parsoid_html",
                status="success",
                raw_wikitext_sha256=_raw(),
                source_url="https://example.test/701",
            )
        with pytest.raises(ValueError, match="recorded together"):
            store.record_render_resource(
                revision_id=701,
                resource_kind="parsoid_html",
                status="failed",
                raw_wikitext_sha256=_raw(),
                source_url="https://example.test/701",
                content_sha256=_raw("rejected response"),
            )
        store.record_render_resource(
            revision_id=701,
            resource_kind="parsoid_html",
            status="failed",
            raw_wikitext_sha256=_raw(),
            source_url="https://example.test/701",
            http_metadata={"status": 503},
            error={"reason": "upstream_timeout"},
        )

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE render_resource SET http_metadata_json = ? "
        "WHERE revision_id = 701 AND resource_kind = 'parsoid_html'",
        ('{"status":200}',),
    )
    connection.commit()
    connection.close()

    with (
        DiscussionToolsStateStore(path, _manifest()) as store,
        pytest.raises(RuntimeError, match="HTTP metadata hash mismatch"),
    ):
        store.get_render_resource(701, "parsoid_html")
