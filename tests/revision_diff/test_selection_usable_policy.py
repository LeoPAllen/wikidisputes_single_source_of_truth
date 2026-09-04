import json

import pytest

from wikidisputes_ssot.revision_diff.workflow import monotonic_selection_row


def source(status="fallback", text="A TEXT"):
    return {
        "source_row_uid": "row-1",
        "logical_utterance_uid": "logical-1",
        "action_uid": "action-1",
        "method_a_status": status,
        "method_a_selected_text": text,
    }


def method_b(
    status,
    *,
    body="B TEXT",
    reasons=(),
    assignment_status="assigned",
    ambiguity=(),
):
    return {
        "status": status,
        "candidate_body": body,
        "reason_codes_json": json.dumps(list(reasons)),
        "assignment_status": assignment_status,
        "ambiguity_flags_json": json.dumps(list(ambiguity)),
    }


def test_method_a_promote_remains_immutable_over_b_usable():
    row = monotonic_selection_row(
        source("promote", "TRUSTED A"),
        method_b(
            "b_usable",
            reasons=("structure:terminal_signature",),
        ),
    )
    assert row["selected_method"] == "method_a"
    assert row["selected_text"] == "TRUSTED A"


@pytest.mark.parametrize("a_status", ["fallback", "review"])
def test_b_usable_selected_for_non_promote_a(a_status):
    row = monotonic_selection_row(
        source(a_status),
        method_b(
            "b_usable",
            reasons=("structure:terminal_signature",),
        ),
    )
    assert row["selected_method"] == "method_b"
    assert row["selected_text"] == "B TEXT"
    assert row["method_b_status"] == "b_usable"


def test_b_safe_still_selected():
    row = monotonic_selection_row(
        source("fallback"),
        method_b("b_safe", reasons=()),
    )
    assert row["selected_method"] == "method_b"
    assert row["method_b_status"] == "b_safe"


@pytest.mark.parametrize(
    "status",
    ["b_review", "b_no_candidate", "b_ambiguous", "b_unavailable"],
)
def test_noneligible_b_status_not_selected(status):
    row = monotonic_selection_row(
        source("fallback"),
        method_b(status),
    )
    assert row["selected_method"] == "method_a_fallback"
    assert row["selected_text"] == "A TEXT"


def test_ambiguous_assignment_fails_closed_even_if_status_says_usable():
    row = monotonic_selection_row(
        source("fallback"),
        method_b(
            "b_usable",
            reasons=("structure:terminal_signature",),
            assignment_status="ambiguous",
        ),
    )
    assert row["selected_method"] == "method_a_fallback"


def test_ambiguity_flag_fails_closed_even_if_status_says_usable():
    row = monotonic_selection_row(
        source("fallback"),
        method_b(
            "b_usable",
            reasons=("structure:terminal_signature",),
            ambiguity=("assignment_not_unique",),
        ),
    )
    assert row["selected_method"] == "method_a_fallback"


def test_hard_reason_cannot_enter_b_usable_selection():
    row = monotonic_selection_row(
        source("fallback"),
        method_b(
            "b_usable",
            reasons=(
                "structure:terminal_signature",
                "lifecycle_inconsistent",
            ),
        ),
    )
    assert row["selected_method"] == "method_a_fallback"


def test_unsigned_signature_residue_is_approved_soft_reason():
    row = monotonic_selection_row(
        source("review"),
        method_b(
            "b_usable",
            reasons=("structure:unsigned_signature_residue",),
        ),
    )
    assert row["selected_method"] == "method_b"
