from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IdentityRevert:
    reverting_revision_id: str
    restored_revision_id: str
    reverted_revision_ids: tuple[str, ...]
    sha1: str


def detect_identity_reverts(revisions: list[dict[str, Any]]) -> list[IdentityRevert]:
    """Detect full identity reverts from chronological revision SHA-1 history.

    A revision is a revert when its SHA-1 equals an earlier state and at least one
    different intervening state exists. Revert tags are intentionally not used as
    the decision rule; callers may retain them as corroborating evidence.
    """
    last_index_by_sha1: dict[str, int] = {}
    output: list[IdentityRevert] = []
    for index, revision in enumerate(revisions):
        sha1 = revision.get("sha1")
        revision_id = revision.get("revision_id")
        if not isinstance(sha1, str) or not sha1 or revision_id is None:
            continue
        earlier_index = last_index_by_sha1.get(sha1)
        if earlier_index is not None and index - earlier_index > 1:
            intervening = revisions[earlier_index + 1 : index]
            reverted = tuple(
                str(item["revision_id"])
                for item in intervening
                if item.get("revision_id") is not None and item.get("sha1") != sha1
            )
            if reverted:
                output.append(
                    IdentityRevert(
                        reverting_revision_id=str(revision_id),
                        restored_revision_id=str(revisions[earlier_index]["revision_id"]),
                        reverted_revision_ids=reverted,
                        sha1=sha1,
                    )
                )
        last_index_by_sha1[sha1] = index
    return output
