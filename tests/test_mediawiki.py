from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from wikidisputes_ssot import hydration
from wikidisputes_ssot.hydration import _revision_actor_identity_status
from wikidisputes_ssot.mediawiki import (
    MediaWikiClient,
    _retry_after_seconds,
    revision_availability,
)


def test_revision_batches_request_required_content_properties() -> None:
    client = object.__new__(MediaWikiClient)
    calls: list[dict[str, Any]] = []

    def request(parameters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        calls.append(parameters)
        return {"query": {}}, {"request_hash": "fixture"}

    client.request = request  # type: ignore[method-assign]
    list(client.revisions_by_ids([1, 2, 3], include_content=True, batch_size=2))
    assert [call["revids"] for call in calls] == ["1|2", "3"]
    assert all(call["rvslots"] == "main" for call in calls)
    assert all("content" in call["rvprop"] and "sha1" in call["rvprop"] for call in calls)


def test_revision_availability_keeps_hidden_states_distinct() -> None:
    hidden = revision_availability({}, {"revid": 1, "texthidden": True, "userhidden": True})
    assert hidden["availability_status"] == "suppressed_or_revision_deleted_text"
    assert hidden["userhidden"] is True
    assert revision_availability({"missing": True}, None)["availability_status"] == "missing_page"
    assert revision_availability({}, {"revid": 1})["availability_status"] == "metadata_only"


def test_retry_after_supports_seconds_and_http_date() -> None:
    now = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)
    assert _retry_after_seconds("7", now=now) == 7.0
    assert _retry_after_seconds("Wed, 19 Aug 2026 12:00:09 GMT", now=now) == 9.0
    assert _retry_after_seconds("not-a-date", now=now) is None


def test_revision_actor_states_do_not_collapse_ip_temporary_or_hidden() -> None:
    assert _revision_actor_identity_status("192.0.2.1", None, userhidden=False) == (
        "revision_actor_ip_observed"
    )
    assert _revision_actor_identity_status("~2026-12345", 7, userhidden=False) == (
        "revision_actor_temporary_observed"
    )
    assert _revision_actor_identity_status(None, None, userhidden=True) == (
        "revision_actor_hidden_or_deleted"
    )
    assert _revision_actor_identity_status("Renamed User", 42, userhidden=False) == (
        "revision_actor_observed_rename_status_unchecked"
    )


def test_bounded_parse_requests_preserve_revision_order(monkeypatch: Any) -> None:
    def fake_request(job: tuple[Any, int]) -> tuple[int, dict[str, Any], dict[str, Any], None]:
        return job[1], {"parse": {}}, {"request_hash": str(job[1])}, None

    monkeypatch.setattr(hydration, "_parse_request", fake_request)
    settings = SimpleNamespace(network=SimpleNamespace(max_concurrency=2))
    results = list(hydration._bounded_parse_requests(settings, [9, 3, 7]))
    assert [row[0] for row in results] == [9, 3, 7]
