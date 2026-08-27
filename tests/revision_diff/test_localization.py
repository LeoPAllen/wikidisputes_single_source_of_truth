from wikidisputes_ssot.revision_diff.boundaries import extract_comment_candidates
from wikidisputes_ssot.revision_diff.localization import localize_candidates


def _signed(body: str, user: str) -> str:
    return f"{body} -- [[User:{user}]] 12:34, 1 January 2020 (UTC)"


def test_large_page_with_one_changed_comment_localizes_to_small_pool() -> None:
    comments = [_signed(f"Comment {index}.", f"User{index}") for index in range(100)]
    raw = "\n".join(comments)
    candidates = extract_comment_candidates(raw)
    changed_start = raw.index("Comment 50.")
    result = localize_candidates(raw, candidates, [(changed_start, changed_start + len("Comment"))])

    assert result.whole_page_candidate_count == 100
    assert result.localized_candidate_count == 1
    assert result.candidates[0].body_wikitext.strip() == "Comment 50."
    reasons = result.reasons_for(
        result.candidates[0].candidate_uid, (changed_start, changed_start + 7)
    )
    assert reasons == ("changed_target_span_overlaps_or_contains_candidate",)


def test_boundary_whitespace_change_retains_only_adjacent_structural_neighbors() -> None:
    raw = "\n".join((_signed("First.", "A"), _signed("Second.", "B"), _signed("Third.", "C")))
    candidates = extract_comment_candidates(raw)
    boundary_span = (candidates[0].end, candidates[1].start)
    assert raw[boundary_span[0] : boundary_span[1]].isspace()

    result = localize_candidates(raw, candidates, [boundary_span])

    assert [candidate.body_wikitext.strip() for candidate in result.candidates] == [
        "First.",
        "Second.",
    ]
    for candidate in result.candidates:
        assert "immediate_structural_neighbor_at_ambiguous_boundary" in result.reasons_for(
            candidate.candidate_uid, boundary_span
        )


def test_more_than_thirty_directly_changed_candidates_remain_localized() -> None:
    raw = "\n".join(_signed(f"Comment {index}.", f"User{index}") for index in range(31))
    candidates = extract_comment_candidates(raw)
    result = localize_candidates(raw, candidates, [(candidates[0].start, candidates[-1].end)])

    assert result.whole_page_candidate_count == 31
    assert result.localized_candidate_count == 31
    assert all(
        result.reasons_for(candidate.candidate_uid, (candidates[0].start, candidates[-1].end))
        == ("changed_target_span_overlaps_or_contains_candidate",)
        for candidate in result.candidates
    )
