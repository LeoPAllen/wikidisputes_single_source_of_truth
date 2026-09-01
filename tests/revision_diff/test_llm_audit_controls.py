from wikidisputes_ssot.revision_diff.llm_audit_controls import (
    annotate_unavailable,
    select_calibration_controls,
    unavailable_taxonomy,
    validate_calibration_key,
)


def test_unavailable_taxonomy_uses_explicit_availability_and_reason_evidence() -> None:
    assert (
        unavailable_taxonomy({"reason_codes_json": '["target_suppressed"]'}) == "suppressed/deleted"
    )
    assert unavailable_taxonomy({"reason_codes": ["cache_fetch_failed"]}) == "fetch/cache failure"
    assert (
        unavailable_taxonomy({"target_revision_available": False}) == "target revision unavailable"
    )
    assert (
        unavailable_taxonomy({"target_revision_available": True, "predecessor_available": False})
        == "predecessor unavailable only"
    )
    assert unavailable_taxonomy({"target_wikitext": None}) == "missing metadata"
    assert unavailable_taxonomy({"target_revision_id": 2, "action_type": "creation"}) == "other"
    annotated = annotate_unavailable([{"source_row_uid": "x", "target_revision_available": False}])
    assert annotated[0]["unavailable_taxonomy"] == "target revision unavailable"


def _row(uid: str, status: str, lifecycle: str, signature: str = "signed") -> dict[str, object]:
    raw = "prefix accepted suffix"
    return {
        "source_row_uid": uid,
        "method_b_status": status if status.startswith("b_") else None,
        "status": status,
        "selected_method": "method_a_promote" if status == "promote" else "method_b",
        "action_type": lifecycle,
        "target_wikitext": raw,
        "candidate_start": 7,
        "candidate_end": 15,
        "candidate_raw": "accepted",
        "candidate_body": "accepted",
        "provenance": "exact",
        "tier": "raw",
        "signature_author": "A" if signature == "signed" else None,
        "autosigned": signature == "autosigned",
    }


def test_controls_are_disjoint_deterministic_stratified_and_exact() -> None:
    rows = [
        _row("a", "promote", "creation", "signed"),
        _row("safe", "b_safe", "modification", "unsigned"),
        _row("usable", "b_usable", "restoration", "autosigned"),
        _row("extra", "b_safe", "creation", "signed"),
        _row("frozen", "b_safe", "creation", "signed"),
    ]
    first = select_calibration_controls(rows, {"frozen"}, size=4)
    second = select_calibration_controls(reversed(rows), {"frozen"}, size=4)
    assert [row["source_row_uid"] for row in first.rows] == [
        row["source_row_uid"] for row in second.rows
    ]
    assert "frozen" not in {row["source_row_uid"] for row in first.rows}
    assert {row["calibration_control_class"] for row in first.rows} >= {
        "a_safe_raw_promotion",
        "b_safe",
        "b_usable",
    }
    validate_calibration_key(first.rows, first.key)
    assert first.key[0]["accepted_body"] == "accepted"


def test_controls_exclude_invalid_raw_boundaries_and_validate_detects_tampering() -> None:
    valid = _row("valid", "b_safe", "creation")
    invalid = _row("invalid", "b_safe", "creation")
    invalid["candidate_end"] = 14
    selected = select_calibration_controls([valid, invalid], (), size=5)
    assert [row["source_row_uid"] for row in selected.rows] == ["valid"]
    bad_key = [dict(selected.key[0], accepted_body="wrong")]
    try:
        validate_calibration_key(selected.rows, bad_key)
    except ValueError as error:
        assert "does not reproduce" in str(error)
    else:
        raise AssertionError("tampered key was accepted")
