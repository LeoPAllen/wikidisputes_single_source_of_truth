from __future__ import annotations

from wikidisputes_ssot.audit import _near_duplicates


def test_near_duplicate_report_is_diagnostic_and_deterministic() -> None:
    rows = [
        ("a", "one two three four five six seven eight"),
        ("b", "one two three four five six seven eight nine"),
        ("c", "entirely unrelated words live in this sentence today"),
    ]
    first = _near_duplicates(rows)
    second = _near_duplicates(list(reversed(rows)))
    assert first == second
    assert all({row["source_row_uid_a"], row["source_row_uid_b"]} != {"a", "c"} for row in first)
