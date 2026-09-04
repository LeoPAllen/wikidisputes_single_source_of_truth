from __future__ import annotations

import json

from wikidisputes_ssot.revision_diff.models import RevisionText
from wikidisputes_ssot.revision_diff.recovery import recover_revision_actions


def _action(source: str, *, speaker: str = "Alice", uid: str = "x1") -> dict[str, object]:
    return {
        "action_uid": uid,
        "source_occurrence_uids": [f"source-{uid}"],
        "logical_utterance_uid": f"logical-{uid}",
        "action_id_exact": "2.10.10",
        "action_type": "creation",
        "source_text": source,
        "source_provenance_exact": True,
        "parentid_verified": True,
        "wikiconv_speaker": speaker,
    }


def _recover(action: dict[str, object], target: str):
    return recover_revision_actions(
        [action], RevisionText.available("1", ""), RevisionText.available("2", target)
    )[0]


def test_frozen_x1_speaker_mismatch_reaches_production_safe() -> None:
    source = (
        "I came here to get a summary of the skepticism concerning this topic "
        "(please google 'carbon neutral myth' or 'carbon offset myth' for verification). "
        "My NPOV dispute comes from the fact that this article makes no mention of such "
        "a myth. The criticisms section has been entirely neutered. This article needs "
        "a summary of the logic which myth proponents use. Thanks."
    )
    target = (
        "\n\n== NPOV dispute ==\n\n" + source + "[[User:Yeago|Yeago]] 03:58, 13 February 2007 (UTC)"
    )
    result = _recover(_action(source, speaker="Subsume"), target)

    assert result.status == "b_safe"
    assert result.x1_proof_status == "proven"
    assert result.source_body_identity_mode == "exact"
    assert result.speaker_signature_provenance == "mismatch"
    assert result.wikiconv_speaker == "Subsume"
    assert result.signature_author == "Yeago"
    assert result.x1_action_localization_mode == "immediately_preceding_heading_plus_candidate"
    assert "x1_exact_source_existing_candidate_proof" in json.loads(result.assignment_evidence_json)


def test_frozen_x1_signature_format_prefix_reaches_production_safe_without_range_change() -> None:
    source = (
        "Although this article itself is a minefield of POV, OR, uninformative mishmash, "
        "the sources look pretty strong and the topic itself is surely worthy of inclusion. "
        "Thus, I propose we stub the article right down to one line and rebuild, cited "
        "statement by cited statement, from the references. Any thoughts? "
    )
    comment = (
        source
        + '<font color="404040">'
        + '[[User:Skomorokh|<font face="Garamond" color="black">скоморохъ</font>]]</font> '
        + "23:20, 5 February 2008 (UTC)"
    )
    target = "\n\n== Proposal for overhaul ==\n" + comment
    result = _recover(_action(source, speaker="Skomorokh"), target)

    assert result.status == "b_safe"
    assert result.x1_proof_status == "proven"
    assert result.source_body_identity_mode == "terminal_signature_formatting_prefix"
    assert result.speaker_signature_provenance == "match"
    assert result.candidate_raw == comment
    assert target[result.candidate_start : result.candidate_end] == comment


def test_x1_mismatch_does_not_rescue_ambiguity_or_competing_action() -> None:
    first = "Common words here. -- [[User:Bob]] 12:34, 1 January 2020 (UTC)"
    second = "Common words here extra. -- [[User:Carol]] 12:35, 1 January 2020 (UTC)"
    target = "== Topic ==\n\n" + first + "\n" + second
    ambiguous = _recover(_action("Common words here.", speaker="Alice"), target)
    assert ambiguous.assignment_status == "ambiguous"
    assert ambiguous.status != "b_safe"
    assert ambiguous.x1_proof_status is None

    single_target = "== Topic ==\n\n" + first
    actions = [
        _action("Common words here.", speaker="Bob", uid="one"),
        _action("Common words here.", speaker="Bob", uid="two"),
    ]
    competing = recover_revision_actions(
        actions, RevisionText.available("1", ""), RevisionText.available("2", single_target)
    )
    assert all(result.x1_proof_status is None for result in competing)
    assert all(result.status != "b_safe" for result in competing)


def test_x1_signature_prefix_rejects_arbitrary_or_unbound_html_and_wrong_lifecycle() -> None:
    source = "Exact source. "
    arbitrary = "\n== Topic ==\n" + source + "<div>[[User:Alice]] 12:34, 1 January 2020 (UTC)"
    arbitrary_result = _recover(_action(source), arbitrary)
    assert arbitrary_result.status != "b_safe"
    assert arbitrary_result.x1_proof_status is None

    unbound = (
        "\n== Topic ==\n" + source + '<font color="navy">[[User:Alice]] 12:34, 1 January 2020 (UTC)'
    )
    unbound_result = _recover(_action(source), unbound)
    assert unbound_result.status != "b_safe"
    assert unbound_result.x1_proof_status is None

    valid_prefix = (
        "\n== Topic ==\n"
        + source
        + '<font color="navy">[[User:Alice]]</font> 12:34, 1 January 2020 (UTC)'
    )
    modification = _action(source)
    modification["action_type"] = "modification"
    lifecycle_result = _recover(modification, valid_prefix)
    assert lifecycle_result.source_body_identity_mode == "terminal_signature_formatting_prefix"
    assert lifecycle_result.status != "b_safe"
    assert lifecycle_result.x1_proof_status is None
