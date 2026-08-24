"""Guards for immutable canonical source-occurrence text."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def source_text_sha256(value: Any) -> str:
    return hashlib.sha256(_text(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceProvenanceCheck:
    checked_rows: int
    unique_source_rows: int
    missing_source_rows: tuple[str, ...]
    duplicate_source_rows: tuple[str, ...]
    text_mismatches: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (
            self.missing_source_rows
            or self.duplicate_source_rows
            or self.text_mismatches
        )

    def require_ok(self, *, label: str = "recovery input") -> None:
        if self.ok:
            return
        details = []
        if self.missing_source_rows:
            details.append(f"missing={list(self.missing_source_rows[:5])}")
        if self.duplicate_source_rows:
            details.append(f"duplicates={list(self.duplicate_source_rows[:5])}")
        if self.text_mismatches:
            details.append(f"text_mismatches={list(self.text_mismatches[:5])}")
        raise RuntimeError(
            f"Canonical source-text provenance failed for {label}: " + "; ".join(details)
        )


def check_source_text_provenance(
    rows: Iterable[Mapping[str, Any]],
    canonical_text_by_source_uid: Mapping[str, Any],
    *,
    uid_field: str = "source_row_uid",
    text_field: str = "source_text",
) -> SourceProvenanceCheck:
    """Compare exact recovery targets with the canonical source occurrence.

    Text is deliberately compared byte-for-byte after conversion to ``str``.
    Annotation/display text is downstream evidence and must never replace the
    immutable canonical source text used as the recovery target.
    """

    seen: set[str] = set()
    duplicates: set[str] = set()
    missing: list[str] = []
    mismatches: list[str] = []
    checked = 0
    for row in rows:
        checked += 1
        uid = _text(row.get(uid_field))
        if uid in seen:
            duplicates.add(uid)
        seen.add(uid)
        if uid not in canonical_text_by_source_uid:
            missing.append(uid)
            continue
        if _text(row.get(text_field)) != _text(canonical_text_by_source_uid[uid]):
            mismatches.append(uid)
    return SourceProvenanceCheck(
        checked_rows=checked,
        unique_source_rows=len(seen),
        missing_source_rows=tuple(sorted(set(missing))),
        duplicate_source_rows=tuple(sorted(duplicates)),
        text_mismatches=tuple(sorted(set(mismatches))),
    )
