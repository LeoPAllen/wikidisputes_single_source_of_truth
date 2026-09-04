from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "recover_raw_mediawiki_comments.py"

SPEC = importlib.util.spec_from_file_location(
    "recover_raw_mediawiki_comments",
    SCRIPT,
)
assert SPEC is not None
assert SPEC.loader is not None
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)


def test_canonical_candidates_are_unchanged_when_nonempty() -> None:
    text = "Comment. --[[User:Alice|Alice]] 12:34, 8 July 2005 (UTC)\n"

    original = RECOVERY.candidate_comments(text)

    assert len(original) == 1
    assert original[0]["raw"] == text[:-1]
    assert original[0]["start"] == 0
    assert original[0]["end"] == len(text) - 1
    assert original[0]["boundary_method"] != "historical_signature_fallback"
    assert "provenance" not in original[0]
    assert "tier" not in original[0]


def test_historical_fallback_runs_only_when_canonical_candidates_are_absent() -> None:
    text = "Historical comment. --[[User:Alice|Alice]] Mar 30, 2005\n"

    candidates = RECOVERY.candidate_comments(text)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["start"] == 0
    assert candidate["end"] == len(text) - 1
    assert candidate["raw"] == text[:-1]
    assert candidate["boundary_method"] == "historical_signature_fallback"
    assert candidate["provenance"] == "historical_signature_fallback"
    assert candidate["tier"] == "historical_signature_fallback"


def test_historical_prose_date_is_rejected() -> None:
    text = "I mentioned [[User:Alice]] on Mar 30, 2005 because that was the date of the edit.\n"

    assert RECOVERY.candidate_comments(text) == []


def test_historical_fallback_strips_signature_from_body() -> None:
    text = "Historical comment. --[[User:Alice|Alice]] 8 July 2005 00:46\n"

    candidate = RECOVERY.candidate_comments(text)[0]

    assert candidate["raw"] == text[:-1]
    assert candidate["body_without_signature"] == "Historical comment."
