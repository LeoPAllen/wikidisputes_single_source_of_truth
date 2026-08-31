from wikidisputes_ssot.revision_diff.diff_span_boundary import diff_span_structural


def _signed(body: str, user: str) -> str:
    return f"{body} -- [[User:{user}]] 12:34, 1 January 2020 (UTC)"


def test_unsigned_page_edge_candidate_keeps_raw_and_body_identical() -> None:
    raw = "Changed unsigned prose."
    candidate = diff_span_structural(raw, [(0, len("Changed"))])

    assert candidate is not None
    assert candidate.raw_wikitext == raw
    assert candidate.body_wikitext == raw
    assert candidate.raw_range == (0, len(raw))
    assert candidate.body_range == candidate.raw_range
    assert candidate.boundary_evidence == (
        "diff_span_structural",
        "preceded_by_page_edge",
        "followed_by_page_edge",
    )


def test_heading_and_incompatible_indent_close_unsigned_region() -> None:
    raw = "== Topic ==\nChanged unsigned prose.\n:Indented reply."
    start = raw.index("Changed")
    candidate = diff_span_structural(raw, [(start, start + len("Changed"))])

    assert candidate is not None
    assert candidate.raw_wikitext == "Changed unsigned prose."
    assert "preceded_by_heading" in candidate.boundary_evidence
    assert "followed_by_incompatible_indentation" in candidate.boundary_evidence


def test_same_depth_blank_neighbor_is_not_a_hard_unsigned_boundary() -> None:
    raw = "Earlier unsigned prose.\n\nChanged unsigned prose."
    start = raw.index("Changed")

    assert diff_span_structural(raw, [(start, start + len("Changed"))]) is None


def test_every_substantive_span_must_fit_one_region() -> None:
    raw = "First unsigned prose.\n\nSecond unsigned prose."
    first = raw.index("First")
    second = raw.index("Second")

    assert (
        diff_span_structural(
            raw,
            [
                (first, first + len("First")),
                (second, second + len("Second")),
            ],
        )
        is None
    )


def test_span_crossing_heading_fails_closed() -> None:
    raw = "Changed prose.\n== Next topic ==\nMore prose."
    start = raw.index("Changed")

    assert diff_span_structural(raw, [(start, len(raw))]) is None


def test_span_crossing_another_signed_comment_fails_closed() -> None:
    raw = "\n".join((_signed("First comment.", "Alice"), _signed("Second comment.", "Bob")))
    first = raw.index("First")
    second_end = raw.index("(UTC)", raw.index("Second")) + len("(UTC)")

    assert diff_span_structural(raw, [(first, second_end)]) is None
