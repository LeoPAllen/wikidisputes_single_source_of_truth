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


def test_x1_clear_positive_and_duplicate_or_competing_source_negative() -> None:
    raw = "== Topic ==\n" + _signed("Exact source.")
    assert probe_x1(_row(raw, "Exact source."))["eligible"] is True
    assert probe_x1(_row(raw + "\nExact source.", "Exact source."))["eligible"] is False
    assert (
        probe_x1(_row(raw, "Exact source.", competing_actions_json='["other"]'))["eligible"]
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
