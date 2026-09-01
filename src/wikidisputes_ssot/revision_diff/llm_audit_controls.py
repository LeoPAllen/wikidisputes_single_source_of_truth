"""Small, deterministic helpers for the residual LLM-audit bundle.

These helpers deliberately only read already-produced rows.  They do not make
recovery or selection decisions, and accept mappings so the bundle writer can
join artifacts without coupling this policy to a parquet schema.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

CALIBRATION_SEED = "20260831"
UNAVAILABLE_TAXONOMY = (
    "target revision unavailable",
    "predecessor unavailable only",
    "suppressed/deleted",
    "fetch/cache failure",
    "missing metadata",
    "other",
)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _first(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and _text(value) != "":
            return value
    return None


def _truthy(value: Any) -> bool:
    return value is True or _text(value).strip().casefold() in {"1", "true", "yes"}


def _falsey(value: Any) -> bool:
    return value is False or _text(value).strip().casefold() in {"0", "false", "no"}


def _reason_text(row: Mapping[str, Any]) -> str:
    values: list[Any] = []
    for name in (
        "reason_codes_json",
        "reason_codes",
        "method_b_reasons",
        "failure_reasons",
        "error",
    ):
        value = row.get(name)
        if isinstance(value, (list, tuple, set)):
            values.extend(value)
        elif value not in (None, ""):
            try:
                decoded = json.loads(_text(value))
            except json.JSONDecodeError:
                values.append(value)
            else:
                values.extend(decoded if isinstance(decoded, list) else [decoded])
    return " ".join(_text(value).casefold() for value in values)


def unavailable_taxonomy(row: Mapping[str, Any]) -> str:
    """Classify an existing ``b_unavailable`` record from explicit evidence.

    The order is intentional: suppression/deletion and fetch errors are more
    specific reason-code evidence than generic availability flags.  Missing
    text alone is never treated as unavailable; only explicit flags/statuses,
    IDs, and existing recovery reasons are considered.
    """

    reasons = _reason_text(row)
    if any(token in reasons for token in ("suppressed", "deleted", "revisiondeleted", "oversight")):
        return "suppressed/deleted"
    if _truthy(
        _first(row, "cache_resolution_failure", "fetch_failure", "cache_fetch_failed")
    ) or any(
        token in reasons for token in ("fetch", "cache", "http", "network", "timeout", "api_error")
    ):
        return "fetch/cache failure"

    target_flag = _first(
        row,
        "target_revision_available",
        "target_available",
        "target_raw_available",
        "target_wikitext_available",
    )
    predecessor_flag = _first(
        row,
        "predecessor_revision_available",
        "predecessor_available",
        "predecessor_raw_available",
        "predecessor_wikitext_available",
    )
    if (
        _falsey(target_flag)
        or "target_revision_unavailable" in reasons
        or "target_unavailable" in reasons
    ):
        return "target revision unavailable"
    if (
        _falsey(predecessor_flag)
        or "predecessor_revision_unavailable" in reasons
        or "predecessor_unavailable" in reasons
    ):
        return "predecessor unavailable only"

    if not _text(_first(row, "target_revision_id", "revision_id")):
        return "missing metadata"
    # A predecessor ID is required except for an explicitly represented root.
    if (
        _first(row, "predecessor_revision_id") is None
        and not _truthy(row.get("is_page_creation"))
        and _text(_first(row, "action_type", "lifecycle")).casefold() != "creation"
    ):
        return "missing metadata"
    return "other"


def annotate_unavailable(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy unavailable rows and add their deterministic taxonomy label."""

    result: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        copied["unavailable_taxonomy"] = unavailable_taxonomy(copied)
        result.append(copied)
    return result


def _uid(row: Mapping[str, Any]) -> str:
    uid = _text(row.get("source_row_uid"))
    if not uid:
        raise ValueError("calibration row has no source_row_uid")
    return uid


def _target_raw(row: Mapping[str, Any]) -> str | None:
    value = _first(row, "target_wikitext", "target_raw_revision", "target_raw", "target_text")
    return None if value is None else _text(value)


def _accepted_boundary(row: Mapping[str, Any]) -> tuple[int, int, str, str] | None:
    raw = _target_raw(row)
    control_class = _control_class(row)
    start = _first(
        row,
        "accepted_start",
        "candidate_start",
        "method_b_left_boundary",
        "method_a_left_boundary",
        "body_start",
    )
    end = _first(
        row,
        "accepted_end",
        "candidate_end",
        "method_b_right_boundary",
        "method_a_right_boundary",
        "body_end",
    )
    if control_class == "a_safe_raw_promotion":
        accepted_raw = _first(row, "accepted_raw", "method_a_candidate_full_raw", "candidate_raw")
        body = _first(row, "accepted_body", "method_a_candidate_raw_body", "candidate_body")
    else:
        accepted_raw = _first(
            row, "accepted_raw", "candidate_raw", "method_b_candidate_full_raw", "candidate_body"
        )
        body = _first(row, "accepted_body", "candidate_body", "method_b_candidate_raw_body")
    if raw is None or start is None or end is None or accepted_raw is None or body is None:
        return None
    try:
        start_i, end_i = int(start), int(end)
    except (TypeError, ValueError):
        return None
    accepted_raw_s, body_s = _text(accepted_raw), _text(body)
    if start_i < 0 or end_i < start_i or end_i > len(raw) or raw[start_i:end_i] != accepted_raw_s:
        return None
    return start_i, end_i, accepted_raw_s, body_s


def _control_class(row: Mapping[str, Any]) -> str | None:
    method_b = _text(_first(row, "method_b_status")).casefold()
    if method_b in {"b_safe", "b_usable"}:
        return method_b
    selected = _text(_first(row, "selected_method")).casefold()
    status = _text(_first(row, "status", "method_a_status", "selection_status")).casefold()
    if selected in {"method_a", "method_a_promote", "method_a_raw", "method_a_safe"} or status in {
        "promote",
        "safe",
        "accepted",
        "a_safe",
    }:
        return "a_safe_raw_promotion"
    return None


def _lifecycle(row: Mapping[str, Any]) -> str:
    return _text(_first(row, "action_type", "lifecycle", "lifecycle_status")) or "unobserved"


def _signature_state(row: Mapping[str, Any]) -> str:
    explicit = _text(row.get("signature_state")).casefold()
    if explicit in {"signed", "unsigned", "autosigned"}:
        return explicit
    if _truthy(_first(row, "autosigned", "is_autosigned", "signature_autosigned")):
        return "autosigned"
    if _first(row, "signature_author", "signature_timestamp") is not None:
        return "signed"
    return "unsigned"


def _rank(seed: str, uid: str) -> str:
    return hashlib.sha256(f"{seed}:{uid}".encode()).hexdigest()


@dataclass(frozen=True)
class CalibrationControlSelection:
    rows: tuple[dict[str, Any], ...]
    key: tuple[dict[str, Any], ...]


def select_calibration_controls(
    rows: Iterable[Mapping[str, Any]],
    frozen_sample_uids: Iterable[str],
    *,
    size: int = 50,
    seed: str = CALIBRATION_SEED,
) -> CalibrationControlSelection:
    """Select deterministic, disjoint controls and construct their exact key.

    Controls without a supplied raw revision whose accepted interval slices to
    the supplied accepted raw text are excluded: a control must be auditable.
    Coarse classes, lifecycle and signature state receive one deterministic
    representative where available before remaining slots are hash-ranked.
    """

    if size < 0:
        raise ValueError("size must be non-negative")
    frozen = {_text(uid) for uid in frozen_sample_uids}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for original in rows:
        row = dict(original)
        uid = _uid(row)
        if uid in seen:
            raise ValueError(f"duplicate calibration source_row_uid: {uid}")
        seen.add(uid)
        control_class = _control_class(row)
        boundary = _accepted_boundary(row)
        if uid in frozen or control_class is None or boundary is None:
            continue
        row["calibration_control_class"] = control_class
        row["calibration_lifecycle"] = _lifecycle(row)
        row["calibration_signature_state"] = _signature_state(row)
        candidates.append(row)

    ranked = sorted(candidates, key=lambda row: (_rank(seed, _uid(row)), _uid(row)))
    selected: list[dict[str, Any]] = []
    selected_uids: set[str] = set()

    def take(predicate: Any) -> None:
        if len(selected) >= size:
            return
        for row in ranked:
            uid = _uid(row)
            if uid not in selected_uids and predicate(row):
                selected.append(row)
                selected_uids.add(uid)
                return

    class_targets = {
        "a_safe_raw_promotion": 18,
        "b_safe": 18,
        "b_usable": 14,
    }
    lifecycle_targets = {
        "a_safe_raw_promotion": {"creation": 6, "modification": 6, "restoration": 6},
        "b_safe": {"creation": 6, "modification": 6, "restoration": 6},
        "b_usable": {"creation": 5, "modification": 5, "restoration": 4},
    }
    # Fill per-lifecycle quotas first.  Missing cells are deliberately not
    # invented: their class's remaining quota is hash-filled below.
    for control_class, target in class_targets.items():
        if len(selected) >= size:
            break
        for lifecycle, count in lifecycle_targets[control_class].items():
            for _ in range(count):
                take(
                    lambda row, control_class=control_class, lifecycle=lifecycle: (
                        row["calibration_control_class"] == control_class
                        and row["calibration_lifecycle"] == lifecycle
                    )
                )
        while (
            sum(row["calibration_control_class"] == control_class for row in selected) < target
            and len(selected) < size
        ):
            before = len(selected)
            take(
                lambda row, control_class=control_class: (
                    row["calibration_control_class"] == control_class
                )
            )
            if len(selected) == before:
                break
    # Include each signature state if present, using a same-class swap so the
    # fixed class quotas remain intact.  The stable ranking resolves ties.
    for signature in ("signed", "unsigned", "autosigned"):
        if any(row["calibration_signature_state"] == signature for row in selected):
            continue
        replacement = next(
            (
                row
                for row in ranked
                if row["calibration_signature_state"] == signature
                and _uid(row) not in selected_uids
            ),
            None,
        )
        if replacement is None:
            continue
        control_class = replacement["calibration_control_class"]
        # Same-class/same-lifecycle replacement preserves both deterministic
        # quotas while exposing every available signature state.
        removable = next(
            (
                row
                for row in reversed(selected)
                if row["calibration_control_class"] == control_class
                and row["calibration_lifecycle"] == replacement["calibration_lifecycle"]
            ),
            None,
        )
        if removable is not None:
            selected[selected.index(removable)] = replacement
            selected_uids.remove(_uid(removable))
            selected_uids.add(_uid(replacement))
    # If fewer than 50 known-safe rows were available, include all that exist.
    for row in ranked:
        if len(selected) >= size:
            break
        if _uid(row) not in selected_uids:
            selected.append(row)
            selected_uids.add(_uid(row))

    key: list[dict[str, Any]] = []
    for row in selected:
        boundary = _accepted_boundary(row)
        if boundary is None:  # Defensive: required above, and prevents bad keys.
            raise ValueError(f"control boundary no longer validates: {_uid(row)}")
        start, end, accepted_raw, body = boundary
        key.append(
            {
                "source_row_uid": _uid(row),
                "accepted_start": start,
                "accepted_end": end,
                "accepted_raw": accepted_raw,
                "accepted_body": body,
                "provenance": _first(row, "accepted_provenance", "provenance", "provenance_tag"),
                "tier": _first(row, "accepted_tier", "tier", "candidate_tier"),
                "calibration_control_class": row["calibration_control_class"],
            }
        )
    return CalibrationControlSelection(rows=tuple(selected), key=tuple(key))


def validate_calibration_key(
    controls: Sequence[Mapping[str, Any]], key: Sequence[Mapping[str, Any]]
) -> None:
    """Raise if a key is not one-to-one with controls or its raw slices differ."""

    control_by_uid = {_uid(row): row for row in controls}
    if len(control_by_uid) != len(controls) or len(key) != len(controls):
        raise ValueError("calibration controls/key must have one unique row per UID")
    for item in key:
        uid = _uid(item)
        row = control_by_uid.get(uid)
        if row is None or _accepted_boundary(row) != (
            int(item["accepted_start"]),
            int(item["accepted_end"]),
            _text(item["accepted_raw"]),
            _text(item["accepted_body"]),
        ):
            raise ValueError(f"calibration key does not reproduce raw boundary: {uid}")
