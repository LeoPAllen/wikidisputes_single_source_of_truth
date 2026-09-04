"""Exact, deliberately conservative boundaries for signed talk-page comments.

The extractor works on the supplied revision text only.  It does not render or
normalise wikitext: all offsets are Python character offsets into that text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)"
)

# Historical talk pages contain legitimate signature-date layouts predating
# consistent modern timestamp formatting. Historical forms remain deliberately
# conservative: candidate extraction still requires explicit nearby user/
# contributions markup and a terminal-looking date.
MONTH_HISTORICAL = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)
TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d"
DAY = r"(?:3[01]|[12]\d|0?[1-9])"
YEAR = r"(?:19|20)\d{2}"

TIMESTAMP_RE = re.compile(
    rf"""
    (?P<canonical>
        \b{TIME},\s+{DAY}\s+{MONTH}\s+{YEAR}\s*\(UTC\)
    )
    |
    (?P<historical>
        (?:
            # 8 July 2005 00:46 [UTC]
            \b{DAY}\s+{MONTH_HISTORICAL}\s+{YEAR}
            (?:\s+{TIME})?
            (?:\s*\(UTC\))?
        |
            # Mar 30, 2005 [00:46] [UTC]
            \b{MONTH_HISTORICAL}\s+{DAY}(?:st|nd|rd|th)?
            \s*,?\s+{YEAR}
            (?:\s+{TIME})?
            (?:\s*\(UTC\))?
        |
            # 2004 Nov 11 [00:46] [UTC]
            \b{YEAR}\s+{MONTH_HISTORICAL}\s+{DAY}
            (?:\s+{TIME})?
            (?:\s*\(UTC\))?
        |
            # 00:46, 8 Jul 2005 [UTC]
            \b{TIME}\s*,?\s+{DAY}\s+{MONTH_HISTORICAL}\s+{YEAR}
            (?:\s*\(UTC\))?
        |
            # 00:46, 2005 Jul 8 [UTC]
            \b{TIME}\s*,?\s+{YEAR}\s+{MONTH_HISTORICAL}\s+{DAY}
            (?:\s*\(UTC\))?
        )
    )
    """,
    re.I | re.X,
)


# Exact pre-historical signature recognizers.  These are used for the first,
# compatibility-preserving extraction pass.
LEGACY_TIMESTAMP_RE = re.compile(
    rf"(?P<canonical>"
    rf"\b\d{{1,2}}:\d{{2}},\s+\d{{1,2}}\s+{MONTH}\s+\d{{4}}\s*\(UTC\)"
    rf")|(?P<historical>(?!)x)",
    re.I,
)

LEGACY_USER_LINK_RE = re.compile(
    r"\[\[\s*"
    r"(?P<kind>User(?:\s+talk)?|Special:Contributions)"
    r"\s*[:/]\s*(?P<name>[^\]|#]+)",
    re.I,
)

USER_LINK_RE = re.compile(
    r"\[\[\s*(?P<kind>"
    r"User(?:[ _]+talk)?"
    r"|Special\s*:\s*Contributions"
    r")\s*[:/]\s*(?P<name>[^\]|#]+)",
    re.I,
)

HEADING_RE = re.compile(r"^\s*={2,6}\s*[^=\n].*?={2,6}\s*$")
PAGE_TEMPLATE_RE = re.compile(r"^\s*\{\{\s*(?:Talk\s+header|WikiProject|Article\s+history)\b", re.I)
TEMPLATE_RE = re.compile(r"^\s*\{\{")


@dataclass(frozen=True)
class BoundaryCandidate:
    """A candidate with raw and signature-stripped ranges in target wikitext."""

    candidate_uid: str
    start: int
    end: int
    raw_wikitext: str
    body_start: int
    body_end: int
    body_wikitext: str
    signature_start: int | None
    signature_end: int | None
    raw_signature_wikitext: str | None
    signature_timestamp: str | None
    signature_user_target: str | None
    indentation: str
    depth: int
    boundary_evidence: tuple[str, ...]
    boundary_warnings: tuple[str, ...]

    @property
    def raw_range(self) -> tuple[int, int]:
        return (self.start, self.end)

    @property
    def body_range(self) -> tuple[int, int]:
        return (self.body_start, self.body_end)


def _line_records(text: str) -> list[tuple[int, int, str]]:
    return [
        (match.start(), match.end(), match.group(0))
        for match in re.finditer(r".*(?:\n|$)", text)
        if match.group(0)
    ]


def _indentation(line: str) -> str:
    return re.match(r"^[ \t:*#;]*", line).group(0)  # type: ignore[union-attr]


def _depth(line: str) -> int:
    # Colons are the normal discussion nesting marker; list markup is retained
    # too so it can be audited without pretending to know a visual layout.
    return sum(ch in ":*#;" for ch in _indentation(line))


def _timestamp_is_terminal(
    text: str,
    timestamp: re.Match[str],
) -> bool:
    """Require noncanonical dates to terminate like signatures.

    Canonical UTC timestamps retain the existing behavior. Historical date
    layouts are accepted only when the remainder of their line contains no
    substantive prose.
    """
    if timestamp.group("historical") is None:
        return True

    line_end = text.find("\n", timestamp.end())

    if line_end < 0:
        line_end = len(text)

    tail = text[timestamp.end():line_end]

    # Harmless material sometimes follows old signatures.
    tail = re.sub(
        r"<!--.*?-->",
        "",
        tail,
        flags=re.S,
    )

    tail = re.sub(
        r"</?(?:small|span|sup|sub)\b[^>]*>",
        "",
        tail,
        flags=re.I,
    )

    tail = re.sub(
        r"&nbsp;",
        "",
        tail,
        flags=re.I,
    )

    return bool(
        re.fullmatch(
            r"""[\s\]\[(){}'"`.,;:!?~*/\\\-\u2013\u2014]*""",
            tail,
        )
    )


def _signature_start(text: str, timestamp: re.Match[str], floor: int) -> int | None:
    """Find explicit signature markup near a timestamp, never inventing one."""
    user_link_re = (
        LEGACY_USER_LINK_RE
        if timestamp.re is LEGACY_TIMESTAMP_RE
        else USER_LINK_RE
    )
    window_start = max(floor, timestamp.start() - 700)
    links = list(user_link_re.finditer(text, window_start, timestamp.start()))
    if not links:
        return None
    # A user link immediately before a date is signature evidence.  A link in
    # prose far earlier in a long comment is intentionally not treated as one.
    link = links[-1]
    if timestamp.start() - link.start() > 320:
        return None
    start = link.start()
    introducer = re.search(r"(?:--+|[\u2013\u2014])\s*$", text[floor:start])
    return floor + introducer.start() if introducer else start


def _first_nonblank_line_start(
    lines: list[tuple[int, int, str]], start_index: int, end_index: int
) -> int:
    """Return the first content line after a structural boundary.

    Blank separators belong neither to the preceding signed comment nor to the
    following candidate.  They are retained only when they occur *within* a
    merged unsigned paragraph sequence.
    """
    for index in range(start_index, end_index + 1):
        if lines[index][2].strip():
            return lines[index][0]
    return lines[end_index][0]


def _extract_structural_comment_candidates(
    target_wikitext: str,
    *,
    timestamp_re: re.Pattern[str],
) -> list[BoundaryCandidate]:
    """Return signed candidates, preserving exactly the selected raw interval.

    Timestamp-only lines are reported as malformed and omitted.  This is more
    useful than silently assigning a potentially unrelated paragraph.
    """
    text = target_wikitext
    lines = _line_records(text)
    candidates: list[BoundaryCandidate] = []
    prior_end = 0
    for timestamp in timestamp_re.finditer(text):
        if not _timestamp_is_terminal(text, timestamp):
            continue
        line_index = next(
            (i for i, (a, b, _) in enumerate(lines) if a <= timestamp.start() < b),
            None,
        )
        if line_index is None:
            continue
        line_start, _line_end, line = lines[line_index]
        sig_start = _signature_start(text, timestamp, line_start)
        if sig_start is None:
            continue
        # Do not let one timestamp terminate a candidate already consumed by an
        # adjacent signature; malformed doubled dates remain unassigned.
        if sig_start < prior_end:
            continue
        candidate_start = line_start
        evidence = ["terminal_explicit_user_link"]

        if timestamp.group("historical") is None:
            evidence.append("terminal_utc_timestamp")
        else:
            evidence.append("terminal_historical_timestamp")
        warnings: list[str] = []
        candidate_depth = _depth(line)
        boundary_found = False
        for j in range(line_index - 1, -1, -1):
            previous_start, _previous_end, previous = lines[j]
            stripped = previous.strip()
            if not stripped:
                # Blank lines are weak boundaries: an unsigned paragraph at
                # the same nesting level may be part of this signed comment.
                # Keep scanning so repeated blank separators work too.
                continue
            if previous_start < prior_end:
                candidate_start = _first_nonblank_line_start(lines, j + 1, line_index)
                evidence.append("preceded_by_prior_candidate")
                boundary_found = True
                break
            if HEADING_RE.match(previous.rstrip("\n")):
                candidate_start = _first_nonblank_line_start(lines, j + 1, line_index)
                evidence.append("preceded_by_heading")
                boundary_found = True
                break
            if PAGE_TEMPLATE_RE.match(previous) or TEMPLATE_RE.match(previous):
                candidate_start = _first_nonblank_line_start(lines, j + 1, line_index)
                evidence.append("preceded_by_page_template")
                boundary_found = True
                break
            # A preceding signed line is a hard neighbour boundary even with no
            # blank line (the usual talk-page reply form).
            prior_timestamp = next(
                (
                    match
                    for match in timestamp_re.finditer(previous)
                    if _timestamp_is_terminal(previous, match)
                ),
                None,
            )
            if (
                prior_timestamp
                and _signature_start(text, prior_timestamp, previous_start) is not None
            ):
                candidate_start = _first_nonblank_line_start(lines, j + 1, line_index)
                evidence.append("preceded_by_signed_neighbor")
                boundary_found = True
                break
            previous_depth = _depth(previous)
            if previous_depth != candidate_depth or _indentation(previous) != _indentation(line):
                candidate_start = _first_nonblank_line_start(lines, j + 1, line_index)
                evidence.append("preceded_by_incompatible_indentation")
                boundary_found = True
                break
            candidate_start = previous_start
            evidence.append("merged_preceding_unsigned_same_depth_paragraph")
        if not boundary_found and line_index:
            warnings.append("start_boundary_reached_document_start")
        if candidate_start < prior_end:
            candidate_start = prior_end
            warnings.append("clipped_to_preceding_candidate")
        # The signature ends at the timestamp; comments following it cannot be
        # silently absorbed.  Exclude only the terminating newline from raw.
        candidate_end = timestamp.end()
        body_end = sig_start
        while body_end > candidate_start and text[body_end - 1].isspace():
            body_end -= 1
        signature = text[sig_start:candidate_end]
        user_link_re = (
            LEGACY_USER_LINK_RE
            if timestamp.re is LEGACY_TIMESTAMP_RE
            else USER_LINK_RE
        )
        user = user_link_re.search(signature)
        candidates.append(
            BoundaryCandidate(
                candidate_uid=f"comment:{len(candidates)}:{candidate_start}:{candidate_end}",
                start=candidate_start,
                end=candidate_end,
                raw_wikitext=text[candidate_start:candidate_end],
                body_start=candidate_start,
                body_end=body_end,
                body_wikitext=text[candidate_start:body_end],
                signature_start=sig_start,
                signature_end=candidate_end,
                raw_signature_wikitext=signature,
                signature_timestamp=timestamp.group(0),
                signature_user_target=user.group("name").strip() if user else None,
                indentation=_indentation(line),
                depth=candidate_depth,
                boundary_evidence=tuple(evidence),
                boundary_warnings=tuple(warnings),
            )
        )
        prior_end = candidate_end
    return candidates


def extract_structural_comment_candidates(
    target_wikitext: str,
) -> list[BoundaryCandidate]:
    """Extract comments without perturbing legacy candidate geometry.

    Historical-signature recognition is strictly a fallback.  If the original
    canonical parser finds any candidate, those candidates are returned
    unchanged.  Historical recognition is attempted only on revisions where
    the legacy parser finds zero candidates.
    """
    legacy = _extract_structural_comment_candidates(
        target_wikitext,
        timestamp_re=LEGACY_TIMESTAMP_RE,
    )

    if legacy:
        return legacy

    return _extract_structural_comment_candidates(
        target_wikitext,
        timestamp_re=TIMESTAMP_RE,
    )


# Short aliases keep the API convenient for pipeline callers and tests.
extract_comment_candidates = extract_structural_comment_candidates
find_comment_candidates = extract_structural_comment_candidates


def candidates_as_dicts(candidates: Iterable[BoundaryCandidate]) -> list[dict[str, Any]]:
    """A shallow interoperable projection for row-oriented pipeline code."""
    return [candidate.__dict__.copy() for candidate in candidates]
