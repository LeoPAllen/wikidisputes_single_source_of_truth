from dataclasses import replace

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


def test_unsigned_same_depth_paragraphs_merge_across_blank_lines() -> None:
    text = (
        ":First paragraph.\n\n"
        ":Second paragraph.\n\n\n"
        ":Third paragraph. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)"
    )
    candidates = extract_comment_candidates(text)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.raw_wikitext == text
    assert candidate.body_wikitext == (
        ":First paragraph.\n\n:Second paragraph.\n\n\n:Third paragraph."
    )
    assert "merged_preceding_unsigned_same_depth_paragraph" in candidate.boundary_evidence
    assert candidate.raw_range == (0, len(text))
    assert text[candidate.body_start : candidate.body_end] == candidate.body_wikitext


def test_blank_lines_do_not_absorb_a_preceding_signed_comment() -> None:
    text = (
        ":First signed comment. -- [[User:Z]] 12:30, 1 January 2020 (UTC)\n\n\n"
        ":Unsigned continuation.\n\n"
        ":Second signed comment. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)"
    )
    first, second = extract_comment_candidates(text)

    assert first.raw_wikitext == ":First signed comment. -- [[User:Z]] 12:30, 1 January 2020 (UTC)"
    assert second.raw_wikitext == (
        ":Unsigned continuation.\n\n"
        ":Second signed comment. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)"
    )
    assert "First signed comment" not in second.raw_wikitext
    assert "preceded_by_prior_candidate" in second.boundary_evidence


def test_heading_template_and_depth_change_block_unsigned_paragraph_merge() -> None:
    cases = (
        (
            "== New topic ==\n\n:Unsigned.\n\n:Signed. -- [[User:A]] 12:34, 1 January 2020 (UTC)",
            ":Unsigned.\n\n:Signed. -- [[User:A]] 12:34, 1 January 2020 (UTC)",
            "preceded_by_heading",
        ),
        (
            "{{Talk header}}\n\n:Unsigned.\n\n:Signed. -- [[User:A]] 12:34, 1 January 2020 (UTC)",
            ":Unsigned.\n\n:Signed. -- [[User:A]] 12:34, 1 January 2020 (UTC)",
            "preceded_by_page_template",
        ),
        (
            "Unsigned at root.\n\n:Signed reply. -- [[User:A]] 12:34, 1 January 2020 (UTC)",
            ":Signed reply. -- [[User:A]] 12:34, 1 January 2020 (UTC)",
            "preceded_by_incompatible_indentation",
        ),
    )
    for text, expected_raw, evidence in cases:
        candidate = extract_comment_candidates(text)[0]
        assert candidate.raw_wikitext == expected_raw
        assert evidence in candidate.boundary_evidence


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


def test_a1_fallback_qualifies_unique_casefolded_signature_and_contained_change() -> None:
    text = (
        "Words by Alice. -- [[User:alice|alice]] 8 July 2005 00:46\n"
        "Other words. -- [[User:Bob|Bob]] 9 July 2005 00:47"
    )
    candidate, other = extract_comment_candidates(text)
    duplicate = replace(candidate, candidate_uid=f"{candidate.candidate_uid}:duplicate")
    result = assign_actions_to_candidates(
        [
            {
                "action_uid": "a",
                "wikiconv_speaker": "Alice",
                "changed_ranges": [(candidate.start, candidate.start + 5)],
            }
        ],
        [candidate, duplicate, other],
    )[0]

    assert result.status == "assigned"
    assert result.candidate_uid == candidate.candidate_uid
    assert "a1_exact_signature_speaker_fallback" in result.evidence


def test_a1_fallback_requires_a_parsed_matching_signature() -> None:
    candidate = extract_comment_candidates(
        "Words. -- [[User:Other]] 12:34, 1 January 2020 (UTC)"
    )[0]
    result = assign_actions_to_candidates(
        [{"action_uid": "a", "wikiconv_speaker": "Alice"}], [candidate]
    )[0]
    assert result.status == "ambiguous"
    assert result.warnings == ("equal_global_assignments",)

    missing_signature = replace(candidate, signature_user_target=None)
    result = assign_actions_to_candidates(
        [{"action_uid": "a", "wikiconv_speaker": "Alice"}], [missing_signature]
    )[0]
    assert result.status == "ambiguous"
    assert result.warnings == ("equal_global_assignments",)


def test_a1_fallback_rejects_multiple_matching_representations() -> None:
    first, second = extract_comment_candidates(
        "First. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)\n"
        "Second. -- [[User:alice]] 12:35, 1 January 2020 (UTC)"
    )
    result = assign_actions_to_candidates(
        [{"action_uid": "a", "wikiconv_speaker": "ALICE"}], [first, second]
    )[0]
    assert result.status == "ambiguous"
    assert result.warnings == ("equal_global_assignments",)


def test_a1_fallback_ignores_offset_only_edge() -> None:
    text = (
        "Words. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)\n"
        "Other. -- [[User:Bob]] 12:35, 1 January 2020 (UTC)"
    )
    candidate, other = extract_comment_candidates(text)
    result = assign_actions_to_candidates(
        [
            {"action_uid": "target", "wikiconv_speaker": "Alice"},
            {"action_uid": "offset-only", "offset_hint": candidate.start},
        ],
        [candidate, other],
    )[0]
    assert result.status == "assigned"
    assert result.candidate_uid == candidate.candidate_uid
    assert "a1_exact_signature_speaker_fallback" in result.evidence


def test_a1_fallback_rejects_substantive_contest() -> None:
    text = (
        "Words. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)\n"
        "Other. -- [[User:Bob]] 12:35, 1 January 2020 (UTC)"
    )
    candidate, other = extract_comment_candidates(text)
    result = assign_actions_to_candidates(
        [
            {"action_uid": "target", "wikiconv_speaker": "Alice"},
            {"action_uid": "contest", "changed_ranges": [(0, len(text))]},
        ],
        [candidate, other],
    )[0]
    assert result.status == "ambiguous"
    assert result.warnings == ("equal_global_assignments",)


def test_a1_fallback_does_not_accept_uncontained_changed_span() -> None:
    text = (
        "Words. -- [[User:Alice]] 12:34, 1 January 2020 (UTC)\n"
        "Other. -- [[User:Bob]] 12:35, 1 January 2020 (UTC)"
    )
    candidate, other = extract_comment_candidates(text)
    result = assign_actions_to_candidates(
        [
            {
                "action_uid": "a",
                "wikiconv_speaker": "Alice",
                "changed_ranges": [(candidate.start, other.end)],
            }
        ],
        [candidate, other],
    )[0]
    assert result.status == "ambiguous"
    assert result.warnings == ("equal_global_assignments",)
