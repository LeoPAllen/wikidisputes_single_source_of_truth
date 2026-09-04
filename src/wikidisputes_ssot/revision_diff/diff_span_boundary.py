"""Geometry-only unsigned candidates bounded by changed target spans."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .boundaries import BoundaryCandidate, extract_comment_candidates

Span = tuple[int, int]

_HEADING_RE = re.compile(r"^\s*={2,6}\s*[^=\n].*?={2,6}\s*$")
_TEMPLATE_RE = re.compile(r"^\s*\{\{")


def _lines(raw_text: str) -> list[tuple[int, int, int, str]]:
    """Return ``(start, end, content_end, text)`` line records."""
    output: list[tuple[int, int, int, str]] = []
    for match in re.finditer(r".*(?:\n|$)", raw_text):
        text = match.group(0)
        if not text:
            continue
        content = text.rstrip("\r\n")
        output.append((match.start(), match.end(), match.start() + len(content), text))
    return output


def _indentation(line: str) -> str:
    return re.match(r"^[ \t:*#;]*", line).group(0)  # type: ignore[union-attr]


def _depth(line: str) -> int:
    return sum(character in ":*#;" for character in _indentation(line))


def _normalise_spans(raw_text: str, spans: Iterable[Span]) -> list[Span] | None:
    normalised: set[Span] = set()
    for value in spans:
        try:
            start, end = value
        except (TypeError, ValueError):
            return None
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        if start < 0 or end < start or end > len(raw_text):
            return None
        if end > start and raw_text[start:end].strip():
            normalised.add((start, end))
    return sorted(normalised)


def _overlaps(left: Span, right: Span) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _line_indices(lines: list[tuple[int, int, int, str]], span: Span) -> list[int]:
    return [
        index
        for index, (start, end, _content_end, _text) in enumerate(lines)
        if start < span[1] and span[0] < end
    ]


def _barrier_kinds(
    raw_text: str,
    lines: list[tuple[int, int, int, str]],
) -> list[str | None]:
    """Classify hard structural lines and recognizable signed comments."""
    signed = extract_comment_candidates(raw_text)
    kinds: list[str | None] = []
    for start, end, _content_end, text in lines:
        line = text.rstrip("\r\n")
        line_span = (start, end)
        if any(_overlaps(line_span, (candidate.start, candidate.end)) for candidate in signed):
            kinds.append("signed_neighbor")
        elif _HEADING_RE.match(line):
            kinds.append("heading")
        elif _TEMPLATE_RE.match(line):
            kinds.append("page_template")
        else:
            kinds.append(None)
    return kinds


def _closure_evidence(side: str, kind: str) -> str:
    return f"{side}_by_{kind}"


def diff_span_structural(
    raw_text: str, substantive_spans: Iterable[Span]
) -> BoundaryCandidate | None:
    """Return one smallest geometry-only candidate around substantive spans.

    Positive-width whitespace-only spans are ignored. Every remaining span must
    lie in one same-indentation/depth region. A blank line is not a boundary:
    encountering same-depth unsigned prose across one therefore fails closed.
    The helper never strips signatures or infers a body; unsigned candidates
    have ``body_wikitext == raw_wikitext``.
    """
    spans = _normalise_spans(raw_text, substantive_spans)
    if not spans:
        return None
    lines = _lines(raw_text)
    if not lines:
        return None
    kinds = _barrier_kinds(raw_text, lines)

    span_lines: set[int] = set()
    for span in spans:
        indices = _line_indices(lines, span)
        if not indices or any(kinds[index] is not None for index in indices):
            return None
        span_lines.update(indices)

    first = min(span_lines)
    last = max(span_lines)
    anchor_line = lines[first][3]
    anchor_indent = _indentation(anchor_line)
    anchor_depth = _depth(anchor_line)

    # All spans must already be in one uninterrupted region. A heading,
    # template, signed line, blank line, or exact indentation transition
    # between spans is a structural boundary rather than a bridge.
    for index in range(first, last + 1):
        _start, _end, _content_end, line = lines[index]
        if kinds[index] is not None or not line.strip():
            return None
        if _indentation(line) != anchor_indent or _depth(line) != anchor_depth:
            return None

    start_index = first
    start_kind: str | None = None
    blank_seen = False
    index = first - 1
    while index >= 0:
        _start, _end, _content_end, line = lines[index]
        if kinds[index] is not None:
            start_kind = kinds[index]
            break
        if not line.strip():
            blank_seen = True
            index -= 1
            continue
        compatible = _indentation(line) == anchor_indent and _depth(line) == anchor_depth
        if blank_seen:
            # Same-depth unsigned prose separated only by blank space is
            # inherently ambiguous; do not absorb it into a fallback.
            if compatible:
                return None
            start_kind = "incompatible_indentation"
            break
        if not compatible:
            start_kind = "incompatible_indentation"
            break
        start_index = index
        index -= 1
    else:
        start_kind = "page_edge"

    end_index = last
    end_kind: str | None = None
    blank_seen = False
    index = last + 1
    while index < len(lines):
        _start, _end, _content_end, line = lines[index]
        if kinds[index] is not None:
            end_kind = kinds[index]
            break
        if not line.strip():
            blank_seen = True
            index += 1
            continue
        compatible = _indentation(line) == anchor_indent and _depth(line) == anchor_depth
        if blank_seen:
            if compatible:
                return None
            end_kind = "incompatible_indentation"
            break
        if not compatible:
            end_kind = "incompatible_indentation"
            break
        end_index = index
        index += 1
    else:
        end_kind = "page_edge"

    start = lines[start_index][0]
    end = lines[end_index][2]
    if start >= end or any(start > span[0] or span[1] > end for span in spans):
        return None

    evidence = (
        "diff_span_structural",
        _closure_evidence("preceded", start_kind or "page_edge"),
        _closure_evidence("followed", end_kind or "page_edge"),
    )
    raw_candidate = raw_text[start:end]
    return BoundaryCandidate(
        candidate_uid=f"diff_span_structural:{start}:{end}",
        start=start,
        end=end,
        raw_wikitext=raw_candidate,
        body_start=start,
        body_end=end,
        body_wikitext=raw_candidate,
        signature_start=None,
        signature_end=None,
        raw_signature_wikitext=None,
        signature_timestamp=None,
        signature_user_target=None,
        indentation=anchor_indent,
        depth=anchor_depth,
        boundary_evidence=evidence,
        boundary_warnings=(),
    )


__all__ = ["diff_span_structural"]
