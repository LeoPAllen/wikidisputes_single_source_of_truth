from __future__ import annotations

from wikidisputes_ssot.recover import extract_fragment
from wikidisputes_ssot.representations import extract_links, extract_signature_evidence


def test_recovered_link_target_absent_from_projection_text() -> None:
    released = "Read the policy."
    revision = "Read the [[Wikipedia:Neutral point of view|policy]]."
    links = extract_links(revision, logical_utterance_uid="wikiconv:1", version_uid="v1")
    assert "Wikipedia:Neutral point of view" not in released
    assert [(link.raw_target, link.displayed_anchor_text) for link in links] == [
        ("Wikipedia:Neutral point of view", "policy")
    ]


def test_no_link_is_not_invented_from_anchor_words() -> None:
    assert extract_links("Read the policy.", logical_utterance_uid="u", version_uid="v") == []


def test_link_kinds_and_signature_states() -> None:
    text = (
        "[[User talk:Example|talk]] [[Special:Contributions/192.0.2.1]] "
        "https://en.wikipedia.org/w/index.php?oldid=12&diff=13 "
        "12:34, 4 July 2012 (UTC)"
    )
    kinds = {
        link.link_kind for link in extract_links(text, logical_utterance_uid="u", version_uid="v")
    }
    assert {"user_talk", "special_contributions", "diff_revision"} <= kinds
    signature = extract_signature_evidence(text)
    assert signature["signature_status"] == "explicit_evidence_observed"
    assert signature["user_talk_target"] == "Example"
    assert signature["contributions_target"] == "192.0.2.1"
    assert extract_signature_evidence("unsigned comment")["signature_status"] == (
        "not_observed_in_fragment"
    )


def test_fragment_extraction_preserves_markup_and_span() -> None:
    revision = "prefix Read the [[Policy|policy]]. suffix"
    # Position-based recovery is necessary because markup prevents exact visible matching.
    recovered = extract_fragment(revision, "Read the policy.", "1.7.7")
    assert recovered["status"] == "recovered_candidate"
    assert "[[Policy|policy]]" in recovered["fragment"]
    unresolved = extract_fragment("unrelated", "different long comment", "1.999.999")
    assert unresolved["status"] == "unresolved"
