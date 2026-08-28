from wikidisputes_ssot.revision_diff.boundaries import (
    extract_comment_candidates,
)


def only_candidate(text: str):
    candidates = extract_comment_candidates(text)

    assert len(candidates) == 1

    return candidates[0]


def test_canonical_signature_remains_supported():
    candidate = only_candidate(
        "This is the comment. "
        "--[[User:Alice|Alice]] "
        "12:34, 8 July 2005 (UTC)\n"
    )

    assert candidate.signature_user_target == "Alice"
    assert "terminal_utc_timestamp" in candidate.boundary_evidence
    assert (
        "terminal_historical_timestamp"
        not in candidate.boundary_evidence
    )


def test_historical_day_month_year_time():
    candidate = only_candidate(
        "Historical comment. "
        "--[[User:Alice|Alice]] "
        "8 July 2005 00:46\n"
    )

    assert candidate.signature_user_target == "Alice"
    assert candidate.body_wikitext == "Historical comment."
    assert (
        "terminal_historical_timestamp"
        in candidate.boundary_evidence
    )


def test_historical_month_day_year():
    candidate = only_candidate(
        "Historical comment. "
        "--[[User talk:Alice|Alice]] "
        "Mar 30, 2005\n"
    )

    assert candidate.signature_user_target == "Alice"
    assert candidate.body_wikitext == "Historical comment."


def test_historical_year_month_day():
    candidate = only_candidate(
        "Historical comment. "
        "— [[User_talk:Alice|Alice]] "
        "2004 Nov 11\n"
    )

    assert candidate.signature_user_target == "Alice"
    assert candidate.body_wikitext == "Historical comment."


def test_historical_time_first():
    candidate = only_candidate(
        "Historical comment. "
        "--[[User:Alice|Alice]] "
        "00:46, 8 Jul 2005\n"
    )

    assert candidate.signature_user_target == "Alice"


def test_special_contributions_signature():
    candidate = only_candidate(
        "Historical comment. "
        "--[[Special:Contributions:ExampleUser|ExampleUser]] "
        "00:46, 8 Jul 2005\n"
    )

    assert candidate.signature_user_target == "ExampleUser"


def test_date_without_user_link_is_not_signature():
    assert (
        extract_comment_candidates(
            "Something happened on Mar 30, 2005\n"
        )
        == []
    )


def test_date_near_user_link_followed_by_prose_is_not_signature():
    text = (
        "I mentioned [[User:Alice]] on Mar 30, 2005 "
        "because that was the date of the edit.\n"
    )

    assert extract_comment_candidates(text) == []


def test_timestamp_followed_by_sentence_is_not_signature():
    text = (
        "[[User:Alice|Alice]] 8 July 2005 00:46 "
        "was when this happened.\n"
    )

    assert extract_comment_candidates(text) == []


def test_historical_signed_neighbor_is_boundary():
    text = (
        "First comment. "
        "--[[User:Alice|Alice]] Mar 30, 2005\n"
        ":Second comment. "
        "--[[User:Bob|Bob]] Mar 31, 2005\n"
    )

    candidates = extract_comment_candidates(text)

    assert len(candidates) == 2

    assert candidates[0].signature_user_target == "Alice"
    assert candidates[1].signature_user_target == "Bob"

    assert {
        "preceded_by_signed_neighbor",
        "preceded_by_prior_candidate",
    } & set(candidates[1].boundary_evidence)


def test_prose_date_in_previous_line_is_not_signed_neighbor():
    text = (
        "Discussion of [[User:Alice]] on Mar 30, 2005 "
        "because that was an important edit.\n"
        ":Actual comment. "
        "--[[User:Bob|Bob]] Apr 2, 2005\n"
    )

    candidates = extract_comment_candidates(text)

    assert len(candidates) == 1
    assert candidates[0].signature_user_target == "Bob"

    assert (
        "preceded_by_signed_neighbor"
        not in candidates[0].boundary_evidence
    )


def test_user_talk_underscore_is_supported():
    candidate = only_candidate(
        "Comment. "
        "--[[User_talk:Alice|talk]] "
        "Apr 2, 2005\n"
    )

    assert candidate.signature_user_target == "Alice"


def test_historical_parser_does_not_modify_page_with_legacy_candidate():
    text = (
        "Old historical comment. "
        "--[[User:OldEditor|OldEditor]] Mar 30, 2005\n"
        "\n"
        "Canonical comment. "
        "--[[User:CurrentEditor|CurrentEditor]] "
        "12:34, 8 July 2005 (UTC)\n"
    )

    candidates = extract_comment_candidates(text)

    # Historical recognition is fallback-only.  Because the legacy parser
    # already has a canonical candidate, the historical line must not alter
    # candidate geometry or introduce assignment competition.
    assert len(candidates) == 1
    assert (
        candidates[0].signature_user_target
        == "CurrentEditor"
    )
    assert (
        "terminal_historical_timestamp"
        not in candidates[0].boundary_evidence
    )
