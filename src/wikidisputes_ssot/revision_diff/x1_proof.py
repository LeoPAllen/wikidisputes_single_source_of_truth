"""Pure identity/provenance proofs shared by X1 probes and recovery."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .boundaries import HEADING_RE, BoundaryCandidate

_COLON_PREFIX_RE = re.compile(r"^:+[ \t]*")
_SIGNATURE_FORMAT_PREFIX_RE = re.compile(
    r"(?:\s|--+|[\u2013\u2014]|<(?:font|span|small|b)\b[^>]*>)+", re.I
)
_SIGNATURE_FORMAT_TAG_RE = re.compile(r"<(font|span|small|b)\b[^>]*>", re.I)

PRODUCTION_X1_IDENTITY_MODES = frozenset({"exact", "terminal_signature_formatting_prefix"})


def speaker_signature_provenance(frozen_speaker: object, raw_signature_user: object) -> str:
    """Return diagnostic-only signature provenance; mismatch is never evidence."""

    frozen = str(frozen_speaker or "").strip().replace("_", " ").casefold()
    signature = str(raw_signature_user or "").strip().replace("_", " ").casefold()
    if not frozen or not signature:
        return "unknown"
    return "match" if frozen == signature else "mismatch"


def x1_body_identity_mode(
    *,
    candidate_body: object,
    source: object,
    indentation: object = "",
    body_end: int | None = None,
    signature_start: int | None = None,
    raw_signature: object = "",
) -> str | None:
    """Prove one narrow source/body identity mode without changing raw ranges."""

    body = str(candidate_body or "")
    source_text = str(source or "")
    body_trimmed, source_trimmed = body.strip(), source_text.strip()
    if not source_trimmed:
        return None
    outer_whitespace = body != body_trimmed or source_text != source_trimmed
    if body_trimmed == source_trimmed:
        return "outer_whitespace_only" if outer_whitespace else "exact"

    parsed_indentation = str(indentation or "")
    prefix = None
    if (
        parsed_indentation
        and re.fullmatch(r":+[ \t]*", parsed_indentation)
        and body_trimmed.startswith(parsed_indentation)
    ):
        prefix = re.match(rf"^{re.escape(parsed_indentation)}[ \t]*", body_trimmed)
    elif not parsed_indentation:
        prefix = _COLON_PREFIX_RE.match(body_trimmed)
    core = body_trimmed[prefix.end() :].strip() if prefix else body_trimmed
    if prefix and core == source_trimmed:
        return "colon_indentation_only"

    if not core.startswith(source_trimmed):
        return None
    suffix = core[len(source_trimmed) :]
    signature = str(raw_signature or "")
    tags = _SIGNATURE_FORMAT_TAG_RE.findall(suffix)
    if (
        tags
        and _SIGNATURE_FORMAT_PREFIX_RE.fullmatch(suffix)
        and signature
        and signature_start == body_end
        and all(re.search(rf"</{re.escape(tag)}\s*>", signature, re.I) for tag in tags)
    ):
        return (
            "colon_indentation_plus_terminal_signature_formatting_prefix"
            if prefix
            else "terminal_signature_formatting_prefix"
        )
    return None


def x1_creation_localization_mode(
    raw_text: str,
    candidate: BoundaryCandidate,
    substantive_spans: Sequence[tuple[int, int]],
) -> str | None:
    """Prove a creation span is the candidate or one preceding heading plus it."""

    if not substantive_spans:
        return None
    if all(candidate.start <= start and end <= candidate.end for start, end in substantive_spans):
        return "candidate_closed"
    if len(substantive_spans) != 1 or "preceded_by_heading" not in candidate.boundary_evidence:
        return None
    start, end = substantive_spans[0]
    if not (start < candidate.start and end == candidate.end):
        return None
    prefix = raw_text[start : candidate.start]
    nonblank = [line for line in prefix.splitlines() if line.strip()]
    if len(nonblank) != 1 or not HEADING_RE.fullmatch(nonblank[0]):
        return None
    return "immediately_preceding_heading_plus_candidate"
