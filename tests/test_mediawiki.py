from __future__ import annotations

from typing import Any

from wikidisputes_ssot.mediawiki import MediaWikiClient, revision_availability


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
