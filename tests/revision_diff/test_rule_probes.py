from copy import deepcopy

from wikidisputes_ssot.revision_diff.rule_probes import (
    probe_b1,
    probe_c1a,
    probe_m1,
    probe_r1,
    probe_x1,
    summarize_probe_results,
)


def _signed(body: str, user: str = "Alice") -> str:
    return f"{body} -- [[User:{user}]] 12:34, 1 January 2020 (UTC)"


def _row(raw: str, source: str, **extra: object) -> dict[str, object]:
    return {
        "source_row_uid": "u1",
        "source_text": source,
        "target_wikitext": raw,
        "action_uid": "a1",
        "action_count": 1,
        "action_type": "creation",
        "lifecycle_consistency": "target_change_localized",
        "wikiconv_speaker": "Alice",
        "neighboring_comment_contamination": "clean",
        "competing_candidates_json": "[]",
        "competing_actions_json": "[]",
        "status": "b_review",
        "survey_weight": 5,
        **extra,
    }


def _x1_candidate(
    raw: str,
    body: str,
    *,
    indentation: str = "",
    signature: str | None = None,
) -> dict[str, object]:
    return {
        "candidate_uid": "c1",
        "start": 0,
        "end": len(raw),
        "body_start": 0,
        "body_end": len(body),
        "body_wikitext": body,
        "indentation": indentation,
        "signature_start": len(body) if signature else None,
        "signature_end": len(raw) if signature else None,
        "raw_signature_wikitext": signature,
        "signature_user_target": "Alice",
        "boundary_warnings": [],
    }


def test_x1_clear_positive_and_duplicate_or_competing_source_negative() -> None:
    raw = "== Topic ==\n" + _signed("Exact source.")
    assert probe_x1(_row(raw, "Exact source."))["eligible"] is True
    assert probe_x1(_row(raw + "\nExact source.", "Exact source."))["eligible"] is False
    assert (
        probe_x1(_row(raw, "Exact source.", competing_actions_json='["other"]'))["eligible"]
        is False
    )


def test_x1_speaker_mismatch_is_diagnostic_for_x1_and_r1() -> None:
    raw = "== Topic ==\n" + _signed("Exact source.", "Bob")
    row = _row(raw, "Exact source.")
    result = probe_x1(row)
    assert result["eligible"] is True
    assert result["frozen_speaker"] == "Alice"
    assert result["raw_signature_user"] == "Bob"
    assert result["speaker_provenance"] == "mismatch"

    r1 = probe_r1({**row, "reason_codes_json": '["global_assignment_search_limit"]'})
    assert r1["eligible"] is True
    assert r1["speaker_provenance"] == "mismatch"


def test_x1_indent_accepts_only_leading_structural_markup() -> None:
    source = "Exact source."
    raw = _signed(f"::{source}")
    row = _row(
        raw,
        source,
        all_candidates=[_x1_candidate(raw, f"::{source}", indentation="::")],
        selection__selected_method="method_a_fallback",
        selection__selected_text="immutable",
    )
    before = deepcopy(row)
    result = probe_x1(row)
    assert result["eligible"] is True
    assert result["x1_body_identity"] == "colon_indentation_only"
    assert result["evidence"] == "colon_indentation_only"
    assert row == before

    spaced_raw = _signed(f":: {source}")
    spaced = _row(
        spaced_raw,
        source,
        all_candidates=[_x1_candidate(spaced_raw, f":: {source}", indentation=":: ")],
    )
    assert probe_x1(spaced)["x1_body_identity"] == "colon_indentation_only"

    diagnostic = probe_x1({**row, "lifecycle_consistency": "unresolved"})
    assert diagnostic["eligible"] is False
    assert diagnostic["x1_body_identity"] == "colon_indentation_only"
    competing = probe_x1({**row, "competing_actions_json": '["other"]'})
    assert competing["eligible"] is False
    assert competing["blocker"] == "competing_candidate_or_action"

    for prefix in ("*", "#", ";", ":#"):
        mixed_raw = _signed(f"{prefix}{source}")
        assert (
            probe_x1(
                _row(
                    mixed_raw,
                    source,
                    all_candidates=[
                        _x1_candidate(mixed_raw, f"{prefix}{source}", indentation=prefix)
                    ],
                )
            )["eligible"]
            is False
        )
    misleading = _row(
        _signed(f":{source}"),
        source,
        all_candidates=[_x1_candidate(_signed(f":{source}"), f":{source}", indentation="*")],
    )
    assert probe_x1(misleading)["eligible"] is False


def test_x1_outer_whitespace_identity_is_explicit_and_raw_bounds_unchanged() -> None:
    source = "Exact source."
    body = "  Exact source. \n"
    raw = _signed(body)
    candidate = _x1_candidate(raw, body)
    result = probe_x1(_row(raw, source, all_candidates=[candidate]))
    assert result["eligible"] is True
    assert result["x1_body_identity"] == "outer_whitespace_only"
    assert result["raw_bounds"] == [0, len(raw)]


def test_x1_indent_rejects_internal_or_trailing_material() -> None:
    source = "Exact source."
    internal_raw = _signed("::Exact  source.")
    assert (
        probe_x1(
            _row(
                internal_raw,
                source,
                all_candidates=[_x1_candidate(internal_raw, "::Exact  source.")],
            )
        )["eligible"]
        is False
    )
    trailing_raw = _signed(f"::{source} Extra.")
    assert (
        probe_x1(
            _row(
                trailing_raw,
                source,
                all_candidates=[_x1_candidate(trailing_raw, f"::{source} Extra.")],
            )
        )["eligible"]
        is False
    )


def test_x1_indent_rejects_signature_fragment_difference() -> None:
    source = "Exact source."
    raw = _signed(f"::{source} -- [[User:Alice]]")
    result = probe_x1(
        _row(
            raw,
            source,
            all_candidates=[_x1_candidate(raw, f"::{source} -- [[User:Alice]]")],
        )
    )
    assert result["eligible"] is False
    assert result["blocker"] == "candidate_body_not_exact_source"


def test_x1_accepts_only_demonstrated_terminal_signature_formatting_prefix() -> None:
    source = "Exact source."
    fragment = ' --<font color="navy">'
    signature = "[[User:Alice|Alice]]</font> 12:34, 1 January 2020 (UTC)"
    body = source + fragment
    raw = body + signature
    candidate = _x1_candidate(raw, body, signature=signature)
    result = probe_x1(_row(raw, source, all_candidates=[candidate]))
    assert result["eligible"] is True
    assert result["x1_body_identity"] == "terminal_signature_formatting_prefix"
    assert result["raw_bounds"] == [0, len(raw)]

    substantive = " neighboring words" + fragment
    bad_body = source + substantive
    bad_raw = bad_body + signature
    assert (
        probe_x1(
            _row(
                bad_raw,
                source,
                all_candidates=[_x1_candidate(bad_raw, bad_body, signature=signature)],
            )
        )["eligible"]
        is False
    )
    unclosed_signature = "[[User:Alice|Alice]] 12:34, 1 January 2020 (UTC)"
    unclosed_raw = body + unclosed_signature
    assert (
        probe_x1(
            _row(
                unclosed_raw,
                source,
                all_candidates=[_x1_candidate(unclosed_raw, body, signature=unclosed_signature)],
            )
        )["eligible"]
        is False
    )


def test_c1a_clean_signed_creation_and_adjacent_comment_negative() -> None:
    raw = _signed("Earlier.", "Bob") + "\n" + "Created. -- [[User:Alice]] 8 Jul 2005 00:46"
    start = raw.index("Created.")
    row = _row(raw, "Created.", action_target_changed_ranges_json=[[start, start + 8]])
    assert probe_c1a(row)["eligible"] is True
    adjacent = _signed("Earlier.", "Bob") + "\n" + "Created. -- [[User:Alice]] 8 Jul 2005 00:46"
    start = adjacent.rindex("Created.")
    assert (
        probe_c1a(_row(adjacent, "Created.", action_target_changed_ranges_json=[[0, start + 8]]))[
            "eligible"
        ]
        is False
    )


def test_m1_autosign_positive_and_wrong_author_or_ambiguous_negative() -> None:
    target = _signed("Unsigned source.")
    row = _row(
        target,
        "Unsigned source.",
        action_type="modification",
        predecessor_wikitext="Unsigned source.",
        action_count=1,
        action_target_changed_ranges_json=[[target.index("--"), len(target)]],
    )
    assert probe_m1(row)["eligible"] is True
    assert probe_m1({**row, "wikiconv_speaker": "Bob"})["eligible"] is False
    assert probe_m1({**row, "competing_candidates_json": '["other"]'})["eligible"] is False


def test_b1_allows_tiny_markup_but_rejects_substantive_boundary() -> None:
    raw = "== Topic ==\n" + _signed("<small> Exact. </small>")
    assert probe_b1(_row(raw, "Exact."))["eligible"] is True
    raw = "== Topic ==\n" + _signed("Substantive Exact.")
    assert probe_b1(_row(raw, "Exact."))["eligible"] is False


def test_r1_only_bypasses_limit_after_x1_and_preserves_summary_overlap() -> None:
    raw = "== Topic ==\n" + _signed("Exact source.")
    row = _row(raw, "Exact source.", reason_codes_json='["global_assignment_search_limit"]')
    r1 = probe_r1(row)
    assert r1["eligible"] is True
    assert probe_r1({**row, "competing_actions_json": '["other"]'})["eligible"] is False
    assert probe_r1({**row, "action_type": "restoration"})["eligible"] is False
    assert (
        probe_r1({**row, "reason_codes_json": '["equal_global_assignments"]'})["eligible"] is False
    )
    summary = summarize_probe_results([row], [probe_x1(row), r1])
    assert summary["rule_families"]["X1"]["weighted_residual_rows"] == 5
    assert summary["overlaps"]["X1|R1"]["sample_hits"] == 1
