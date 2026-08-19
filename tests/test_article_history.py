from __future__ import annotations

from typing import Any

from wikidisputes_ssot.article_history import _resolve_title_batch


class FakeClient:
    def request(self, parameters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        assert parameters["redirects"] == "1"
        return (
            {
                "query": {
                    "normalized": [{"from": "foo_bar", "to": "Foo bar"}],
                    "redirects": [
                        {"from": "Foo bar", "to": "Destination"},
                        {"from": "Other", "to": "Destination"},
                    ],
                    "pages": [{"pageid": 42, "ns": 0, "title": "Destination"}],
                }
            },
            {
                "request_hash": "a" * 64,
                "blob_path": "sha256/aa/blob.json.gz",
                "content_sha256": "b" * 64,
                "retrieved_at_utc": "2026-08-19T00:00:00+00:00",
            },
        )


def test_title_resolution_preserves_each_requested_alias() -> None:
    rows, resolved = _resolve_title_batch(FakeClient(), ["foo_bar", "Other"])  # type: ignore[arg-type]
    assert len(rows) == 2
    assert set(resolved) == {"foo_bar", "Other"}
    assert {row["page_id"] for row in rows} == {42}
    assert all(row["resolution_status"] == "resolved" for row in rows)
