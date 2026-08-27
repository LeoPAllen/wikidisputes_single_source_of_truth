from wikidisputes_ssot.revision_diff.assignment import assign_actions_to_candidates
from wikidisputes_ssot.revision_diff.boundaries import extract_comment_candidates


def test_clean_comment_preserves_exact_raw_and_body_ranges() -> None:
    text = (
        "== Topic ==\n\nI agree with this proposal. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)\n"
    )
    candidate = extract_comment_candidates(text)[0]
    assert text[candidate.start : candidate.end] == candidate.raw_wikitext
    assert candidate.body_wikitext == "I agree with this proposal."
    assert candidate.signature_user_target == "Alice"


def test_small_change_can_select_full_comment_boundary() -> None:
    text = "A detailed comment with a changed word. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)"
    candidate = extract_comment_candidates(text)[0]
    action = {"action_uid": "a", "action_type": "modification", "changed_ranges": [(24, 31)]}
    result = assign_actions_to_candidates([action], [candidate])[0]
    assert result.status == "assigned" and result.candidate_uid == candidate.candidate_uid


def test_multiline_comment_expands_back_to_independent_blank_boundary() -> None:
    text = (
        "Unrelated signed comment. -- [[User:Z]] 12:30, 1 January 2020 (UTC)\n\n"
        "First line of one comment.\n"
        "Second line. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)"
    )
    candidate = extract_comment_candidates(text)[1]
    assert candidate.body_wikitext == "First line of one comment.\nSecond line."
    assert "Unrelated signed comment" not in candidate.raw_wikitext


def test_multiple_actions_receive_distinct_candidates_globally() -> None:
    text = (
        "First words. -- [[User:A]] 12:34, 1 January 2020 (UTC)\n"
        "Second words. -- [[User:B]] 12:35, 1 January 2020 (UTC)"
    )
    candidates = extract_comment_candidates(text)
    results = assign_actions_to_candidates(
        [
            {"action_uid": "one", "changed_ranges": [(0, 5)]},
            {"action_uid": "two", "changed_ranges": [(62, 68)]},
        ],
        candidates,
    )
    assert {result.candidate_uid for result in results} == {
        candidate.candidate_uid for candidate in candidates
    }


def test_competing_plausible_actions_are_ambiguous() -> None:
    candidate = extract_comment_candidates("Text. -- [[User:A]] 12:34, 1 January 2020 (UTC)")[0]
    results = assign_actions_to_candidates(
        [
            {"action_uid": "one", "changed_ranges": [(0, 4)]},
            {"action_uid": "two", "changed_ranges": [(0, 4)]},
        ],
        [candidate],
    )
    assert {result.status for result in results} == {"ambiguous"}


def test_malformed_timestamp_without_user_signature_is_not_candidate() -> None:
    assert not extract_comment_candidates("A remark 12:34, 1 January 2020 (UTC)")


def test_adjacent_comments_do_not_absorb_neighbor() -> None:
    text = (
        "One. -- [[User:A]] 12:34, 1 January 2020 (UTC)\n"
        "Two. -- [[User:B]] 12:35, 1 January 2020 (UTC)"
    )
    first, second = extract_comment_candidates(text)
    assert "Two." not in first.raw_wikitext
    assert first.end <= second.start


def test_speaker_signature_match_is_evidence_not_revision_actor_rewrite() -> None:
    candidate = extract_comment_candidates("Words. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)")[
        0
    ]
    result = assign_actions_to_candidates(
        [
            {
                "action_uid": "a",
                "speaker": "Alice",
                "revision_actor": "Different editor",
                "offset_hint": 2,
            }
        ],
        [candidate],
    )[0]
    assert "speaker_matches_signature_target" in result.evidence


def test_actions_without_localized_edges_do_not_expand_the_search_graph() -> None:
    candidate = extract_comment_candidates("Words. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)")[
        0
    ]
    actions = [
        {
            "action_uid": "relevant",
            "changed_ranges": [(0, 5)],
            "localized_candidate_uids": [candidate.candidate_uid],
        },
        *[
            {
                "action_uid": f"irrelevant-{index}",
                "localized_candidate_uids": [],
            }
            for index in range(20)
        ],
    ]
    results = assign_actions_to_candidates(actions, [candidate])
    by_action = {result.action_uid: result for result in results}
    assert by_action["relevant"].status == "assigned"
    assert {result.status for result in results[1:]} == {"unmatched"}
    assert all(
        "revision_too_large_for_safe_global_assignment" not in result.warnings for result in results
    )
