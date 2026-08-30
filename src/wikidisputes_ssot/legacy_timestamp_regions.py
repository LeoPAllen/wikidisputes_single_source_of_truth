"""Read-only candidates from the pre-boundary-v2 Method-A segmenter.

This module is a deliberately narrow preservation of the candidate geometry in
``scripts/recover_raw_mediawiki_comments.py`` at
``858e4bb111068f96a77576e7f4d4f742dff9acb9``.  It is a hypothesis source,
not a ranking, classification, or promotion mechanism.  A caller must run any
returned byte slice through the current Method-A rank/classify path and the
current promotion-safety decision before it can be used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FROZEN_SOURCE_REVISION = "858e4bb111068f96a77576e7f4d4f742dff9acb9"
FROZEN_SOURCE_PATH = "scripts/recover_raw_mediawiki_comments.py"

_MONTH = (
    r"(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)"
)
_TIMESTAMP_RE = re.compile(
    rf"""
    \b
    \d{{1,2}}:\d{{2}},
    \s+
    \d{{1,2}}
    \s+
    {_MONTH}
    \s+
    \d{{4}}
    \s*
    \(UTC\)
    """,
    re.I | re.X,
)
_HEADING_RE = re.compile(r"^\s*=+\s*.*?\s*=+\s*$")


@dataclass(frozen=True)
class LegacyTimestampRegionCandidate:
    """An exact raw slice emitted by the frozen pre-boundary-v2 segmenter."""

    candidate_index: int
    start: int
    end: int
    raw_wikitext: str


def extract_legacy_timestamp_region_candidates(
    raw_wikitext: str,
) -> list[LegacyTimestampRegionCandidate]:
    """Return exactly the frozen timestamp-region candidates.

    This intentionally retains the old parser's broad geometry, including its
    treatment of an intervening heading.  It must therefore be invoked only
    after current, historical, and artifact candidate tiers produce no viable
    result.
    """

    candidates: list[LegacyTimestampRegionCandidate] = []
    previous_end = 0

    for index, timestamp in enumerate(_TIMESTAMP_RE.finditer(raw_wikitext)):
        start = previous_end
        end = timestamp.end()
        fragment = raw_wikitext[start:end]

        consumed = 0
        for line in fragment.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped or _HEADING_RE.match(line):
                consumed += len(line)
                continue
            break

        fragment = fragment[consumed:]
        absolute_start = start + consumed
        if fragment.strip():
            candidates.append(
                LegacyTimestampRegionCandidate(
                    candidate_index=index,
                    start=absolute_start,
                    end=end,
                    raw_wikitext=fragment,
                )
            )
        previous_end = end

    return candidates
