"""Conservative, read-only proof of physical comments in cached revisions.

This evaluator never decides lifecycle identity or infers an author from the
revision actor.  It abstains unless an immutable source row uniquely anchors
one or two explicitly signed, non-overlapping historical spans.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import mwparserfromhell

from .revision_diff.boundaries import (
    HEADING_RE,
    TIMESTAMP_RE,
    USER_LINK_RE,
    BoundaryCandidate,
    extract_structural_comment_candidates,
)

_WORD_RE = re.compile(r"[a-z0-9]+")
_RETROSPECTIVE_ATTRIBUTION = re.compile(
    r"(?:preceding\s+unsigned\s+comment|class\s*=\s*['\"]autosigned['\"]|"
    r"\{\{\s*(?:unsigned|unsignedip|unsigned2)\b)",
    re.I,
)
_QUOTATION_TEMPLATE = re.compile(r"\{\{\s*(?:blockquote|quotation|cquote)\b", re.I)


def _normalized(text: str) -> str:
    rendered = mwparserfromhell.parse(text).strip_code(normalize=True, collapse=True)
    return " ".join(_WORD_RE.findall(rendered.casefold()))


def _similarity(source: str, raw: str) -> float:
    return difflib.SequenceMatcher(
        None, _normalized(source).split(), _normalized(raw).split(), autojunk=False
    ).ratio()


def _signature(candidate: BoundaryCandidate, revision: str) -> tuple[int, str] | None:
    """Choose the first same-author link in a terminal signature cluster."""

    if candidate.signature_timestamp is None or candidate.signature_start is None:
        return None
    timestamp_start = candidate.end - len(candidate.signature_timestamp)
    floor = max(candidate.body_start, timestamp_start - 320)
    links = list(USER_LINK_RE.finditer(revision, floor, timestamp_start))
    if not links:
        return None
    last_base = links[-1].group("name").strip().split("/", 1)[0].casefold()
    compatible = [
        link
        for link in links
        if link.group("name").strip().split("/", 1)[0].casefold() == last_base
    ]
    if not compatible:
        return None
    first = compatible[0]
    author = first.group("name").strip().split("/", 1)[0]
    start = first.start()
    # Retain a preceding dash as signature markup, not comment body.
    introducer = re.search(r"(?:--+|[\u2013\u2014])\s*$", revision[floor:start])
    if introducer:
        start = floor + introducer.start()
    return start, author


def _anchor(source: str, revision: str) -> int | None:
    prefix = source.lstrip(" \t\r\n,:*#;")[:45]
    if len(prefix) < 35 or revision.count(prefix) != 1:
        return None
    return revision.find(prefix)


def _safe_interval(revision: str, start: int, end: int) -> bool:
    raw = revision[start:end]
    if _RETROSPECTIVE_ATTRIBUTION.search(raw):
        return False
    if raw.count("{{") != raw.count("}}"):
        return False
    for timestamp in TIMESTAMP_RE.finditer(raw):
        if USER_LINK_RE.search(raw[max(0, timestamp.start() - 320) : timestamp.start()]):
            return False
    return not any(HEADING_RE.match(line) for line in raw.splitlines())


def overlaps_other_source(result: dict[str, Any], peer_texts: Iterable[str]) -> bool:
    """Abstain if a proposed unit contains another row's substantive opening."""

    raw = result.get("text") or "\n".join(part["text"] for part in result.get("parts", []))
    normalized = _normalized(str(raw))
    for peer in peer_texts:
        words = _normalized(peer).split()
        if len(words) >= 12 and " ".join(words[:12]) in normalized:
            return True
    return False


def evaluate_historical_span(
    source_text: str,
    source_speaker: str,
    revision_text: str,
    revision_id: str,
) -> dict[str, Any] | None:
    """Return exact signed span evidence, or abstain without a unique proof."""

    if (
        not source_text.strip()
        or len(source_text) > 5000
        or not revision_text
        or not revision_id.isdecimal()
    ):
        return None
    anchor = _anchor(source_text, revision_text)
    if anchor is None:
        return None
    start = revision_text.rfind("\n", 0, anchor) + 1
    # The proof concerns the anchored local neighborhood, not every signed
    # comment on a potentially enormous talk page. A span exceeding this
    # window abstains rather than inheriting an unexamined page boundary.
    window_start = max(0, anchor - 8000)
    window_end = min(len(revision_text), anchor + 8000)
    candidates = [
        replace(
            candidate,
            start=candidate.start + window_start,
            end=candidate.end + window_start,
            body_start=candidate.body_start + window_start,
            body_end=candidate.body_end + window_start,
            signature_start=(
                candidate.signature_start + window_start
                if candidate.signature_start is not None
                else None
            ),
            signature_end=(
                candidate.signature_end + window_start
                if candidate.signature_end is not None
                else None
            ),
        )
        for candidate in extract_structural_comment_candidates(
            revision_text[window_start:window_end]
        )
    ]
    following = [candidate for candidate in candidates if candidate.end > anchor]
    if not following:
        return None
    first = following[0]
    if first.start > anchor and first.start - anchor > 6000:
        return None
    if first.start < start and first.end <= anchor:
        return None
    first_signature = _signature(first, revision_text)
    if first_signature is None:
        return None
    first_signature_start, first_author = first_signature
    if not _safe_interval(revision_text, start, first_signature_start):
        return None
    if source_speaker and first_author.casefold() != source_speaker.casefold():
        return None
    first_body = revision_text[start:first_signature_start].strip()
    if not first_body:
        return None
    first_part = {
        "text": first_body,
        "speaker_id": first_author,
        "source_revision_id": revision_id,
        "source_span": [start, first_signature_start],
        "creation_evidence": "unique_source_anchor_and_terminal_signature",
    }
    # A single source row may contain two adjacent signed comments.  Require
    # their *combined* text to explain the row before deriving child units.
    if len(following) > 1:
        second = following[1]
        second_signature = _signature(second, revision_text)
        if (
            second_signature is not None
            and second.start >= first.end
            and not revision_text[first.end : second.start].strip()
            and _safe_interval(revision_text, second.start, second_signature[0])
        ):
            second_signature_start, second_author = second_signature
            second_body = revision_text[second.start : second_signature_start].strip()
            combined = first_body + "\n" + second_body
            if (
                second_body
                and second_author.casefold() != first_author.casefold()
                and _similarity(source_text, combined) >= 0.96
            ):
                return {
                    "kind": "split",
                    "parts": [
                        first_part,
                        {
                            "text": second_body,
                            "speaker_id": second_author,
                            "source_revision_id": revision_id,
                            "source_span": [second.start, second_signature_start],
                            "creation_evidence": "unique_source_anchor_and_terminal_signature",
                        },
                    ],
                    "source_revision_id": revision_id,
                    "source_anchor_offset": anchor,
                    "source_similarity": _similarity(source_text, combined),
                    "second_candidate_start": second.start,
                }
    if _similarity(source_text, first_body) < 0.96:
        return None
    return {
        "kind": "single",
        "text": first_body,
        "speaker_id": first_author,
        "source_revision_id": revision_id,
        "source_span": [start, first_signature_start],
        "source_anchor_offset": anchor,
        "source_similarity": _similarity(source_text, first_body),
        "extended_boundary": start < first.start,
        "extended_by": max(0, first.start - start),
        "extension_reason": (
            "linked_addressee"
            if re.match(
                r"^[ :*#;]*\[\[\s*User(?:[ _]+talk)?\s*:", revision_text[start:anchor], re.I
            )
            else "interior_quotation"
            if _QUOTATION_TEMPLATE.search(revision_text[start : first.start])
            else "other"
        ),
    }
