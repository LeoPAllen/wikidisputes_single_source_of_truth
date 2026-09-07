from __future__ import annotations

from wikidisputes_ssot import method_a_recovery as CURRENT
from wikidisputes_ssot.legacy_timestamp_regions import (
    FROZEN_SOURCE_PATH,
    FROZEN_SOURCE_REVISION,
    extract_legacy_timestamp_region_candidates,
)


def test_frozen_timestamp_region_is_a_distinct_hypothesis_from_current_boundary_parser() -> None:
    text = (
        "Earlier unsigned context.\n"
        "== Later topic ==\n"
        "Signed comment. --[[User:Alice|Alice]] 12:34, 8 July 2005 (UTC)\n"
    )

    legacy = extract_legacy_timestamp_region_candidates(text)
    current = CURRENT.candidate_comments(text)

    assert FROZEN_SOURCE_PATH == "src/wikidisputes_ssot/method_a_recovery.py"
    assert FROZEN_SOURCE_REVISION == "858e4bb111068f96a77576e7f4d4f742dff9acb9"
    assert len(legacy) == 1
    assert legacy[0].start == 0
    assert legacy[0].end == len(text) - 1
    assert legacy[0].raw_wikitext == text[:-1]
    assert all(candidate["raw"] != legacy[0].raw_wikitext for candidate in current)


def test_frozen_parser_keeps_timestamp_regions_separate() -> None:
    text = (
        "== Heading ==\n\n"
        "12:34, 8 July 2005 (UTC)\n"
        "Comment. --[[User:Bob|Bob]] 12:35, 8 July 2005 (UTC)"
    )

    candidates = extract_legacy_timestamp_region_candidates(text)

    assert [candidate.candidate_index for candidate in candidates] == [0, 1]
    assert candidates[0].raw_wikitext == "12:34, 8 July 2005 (UTC)"
    assert candidates[1].raw_wikitext.startswith("Comment.")
