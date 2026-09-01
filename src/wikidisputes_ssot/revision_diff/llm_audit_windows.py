"""Offset-safe, compact raw-evidence windows for LLM audit packets.

The functions here are deliberately independent of the recovery workflow.  They
only project already-frozen row evidence, retaining Python character offsets
into the supplied (unnormalised) revision wikitext.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from wikidisputes_ssot.revision_diff.boundaries import extract_comment_candidates

RawRange = tuple[int, int]


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _first(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _range(value: Any, *, length: int) -> RawRange | None:
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        return None
    try:
        start, end = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    return (start, end) if 0 <= start < end <= length else None


def parse_ranges(value: Any, *, length: int) -> list[RawRange]:
    """Read a JSON/list range field, discarding malformed or out-of-page spans."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, Mapping):
        value = value.get("ranges", value.get("spans", []))
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    direct = _range(value, length=length)
    if direct is not None:
        return [direct]
    return [parsed for item in value if (parsed := _range(item, length=length)) is not None]


def validate_interval_text(raw: str, start: int, end: int, supplied_text: str) -> None:
    """Raise when an advertised raw interval is not an exact text slice."""
    if not (0 <= start < end <= len(raw)):
        raise ValueError(f"invalid raw interval [{start}, {end}) for length {len(raw)}")
    if raw[start:end] != supplied_text:
        raise ValueError(f"raw interval [{start}, {end}) does not match supplied text")


def structural_ranges(raw: str) -> list[RawRange]:
    """Return comment and heading units used to supply local context."""
    ranges = [(item.start, item.end) for item in extract_comment_candidates(raw)]
    ranges.extend(
        (match.start(), match.end())
        for match in re.finditer(r"(?m)^\s*={2,6}\s*[^=\n].*?={2,6}\s*$", raw)
    )
    return sorted(set(ranges))


def structural_units(raw: str) -> list[dict[str, Any]]:
    """Return explicit comment/heading units with their exact raw slices."""
    units = [
        {"kind": "comment", "start": item.start, "end": item.end}
        for item in extract_comment_candidates(raw)
    ]
    units.extend(
        {"kind": "heading", "start": match.start(), "end": match.end()}
        for match in re.finditer(r"(?m)^\s*={2,6}\s*[^=\n].*?={2,6}\s*$", raw)
    )
    for item in units:
        item["raw_text"] = raw[item["start"] : item["end"]]
    return sorted(units, key=lambda item: (item["start"], item["end"], item["kind"]))


def _line_context(raw: str, start: int, end: int) -> RawRange:
    """A three-line fallback when no comment/heading contains a focal span."""
    first = raw.rfind("\n", 0, start)
    first = 0 if first < 0 else first + 1
    previous = raw.rfind("\n", 0, max(0, first - 1))
    first = 0 if previous < 0 else previous + 1
    last = raw.find("\n", end)
    last = len(raw) if last < 0 else last
    following = raw.find("\n", min(len(raw), last + 1))
    last = len(raw) if following < 0 else following
    return (first, last)


def _merge(ranges: Iterable[RawRange]) -> list[RawRange]:
    result: list[RawRange] = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def validate_windows(
    raw: str, focal_ranges: Iterable[RawRange], windows: Iterable[Mapping[str, Any]]
) -> None:
    """Assert that raw windows are exact and collectively contain every focus."""
    checked = list(windows)
    for window in checked:
        validate_interval_text(
            raw, int(window["start"]), int(window["end"]), _text(window["raw_text"])
        )
    for start, end in focal_ranges:
        if not any(int(item["start"]) <= start and end <= int(item["end"]) for item in checked):
            raise ValueError(f"focal interval [{start}, {end}) is absent from raw windows")


def build_raw_windows(
    raw: str,
    focal_ranges: Iterable[RawRange],
    *,
    units: Sequence[RawRange] | None = None,
    context_characters: int = 180,
) -> list[dict[str, Any]]:
    """Build one or more exact raw windows around focal ranges.

    Each focal range is fully retained.  Where a structural unit can be found,
    its immediate previous and next units are included; otherwise adjacent lines
    provide a conservative fallback.  Windows are disjoint after merging.
    """
    valid = [span for item in focal_ranges if (span := _range(item, length=len(raw)))]
    if not raw or not valid:
        return []
    structure = list(units) if units is not None else structural_ranges(raw)
    structure = [span for item in structure if (span := _range(item, length=len(raw)))]
    selected: list[RawRange] = []
    for start, end in valid:
        touching = [
            i for i, (left, right) in enumerate(structure) if left <= end and start <= right
        ]
        if touching:
            left_index, right_index = (
                max(0, touching[0] - 1),
                min(len(structure) - 1, touching[-1] + 1),
            )
            left, right = structure[left_index][0], structure[right_index][1]
        else:
            left, right = _line_context(raw, start, end)
        # Text padding makes sparse pages readable, but can never crop focus.
        left = max(0, min(left, start) - context_characters)
        right = min(len(raw), max(right, end) + context_characters)
        selected.append((left, right))
    windows = _merge(selected)
    result = [{"start": start, "end": end, "raw_text": raw[start:end]} for start, end in windows]
    for window in result:
        validate_interval_text(raw, window["start"], window["end"], window["raw_text"])
    return result


def _candidate_records(row: Mapping[str, Any], raw: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    start, end = _first(row, "candidate_start"), _first(row, "candidate_end")
    try:
        span = _range((start, end), length=len(raw))
    except TypeError:
        span = None
    if span is not None:
        supplied = _text(_first(row, "candidate_raw", "candidate_full_raw"))
        if supplied:
            validate_interval_text(raw, *span, supplied)
        records.append({"start": span[0], "end": span[1], "raw_text": raw[span[0] : span[1]]})
    for value in (
        row.get("all_candidates"),
        row.get("competing_candidate_evidence"),
        row.get("candidate_spans_json"),
    ):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                span = _range(item, length=len(raw))
                if span is not None:
                    records.append(
                        {"start": span[0], "end": span[1], "raw_text": raw[span[0] : span[1]]}
                    )
                continue
            span = _range(
                (
                    item.get("start", item.get("candidate_start")),
                    item.get("end", item.get("candidate_end")),
                ),
                length=len(raw),
            )
            if span is None:
                continue
            supplied = _text(
                item.get("raw_text", item.get("raw_wikitext", item.get("candidate_raw")))
            )
            if supplied:
                validate_interval_text(raw, *span, supplied)
            body_span = _range((item.get("body_start"), item.get("body_end")), length=len(raw))
            body = _text(item.get("body", item.get("body_wikitext", item.get("candidate_body"))))
            if body and body_span is not None:
                validate_interval_text(raw, *body_span, body)
            record: dict[str, Any] = {
                "start": span[0],
                "end": span[1],
                "raw_text": raw[span[0] : span[1]],
            }
            if item.get("candidate_uid") not in (None, ""):
                record["candidate_uid"] = item["candidate_uid"]
            if body_span is not None:
                record.update({"body_start": body_span[0], "body_end": body_span[1], "body": body})
            records.append(record)
    return [dict(item) for item in {(x["start"], x["end"]): x for x in records}.values()]


def _competing_candidate_refs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Reference competing candidates whose full text is in ``candidates``."""

    value = _first(row, "competing_candidate_evidence", "competing_candidates_json") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    fields = ("candidate_uid", "start", "end", "candidate_start", "candidate_end")
    return [
        {key: item[key] for key in fields if item.get(key) not in (None, "")}
        for item in value
        if isinstance(item, Mapping)
    ]


def _neighboring_units(raw: str, focal: Sequence[RawRange]) -> list[dict[str, Any]]:
    """Return the structural unit nearest each focus plus its immediate neighbors."""

    units = structural_units(raw)
    selected: set[int] = set()
    for start, end in focal:
        containing = [
            index
            for index, unit in enumerate(units)
            if unit["start"] <= start and end <= unit["end"]
        ]
        overlapping = [
            index for index, unit in enumerate(units) if unit["start"] < end and start < unit["end"]
        ]
        choices = containing or overlapping
        if not choices:
            continue
        center = min(choices, key=lambda index: units[index]["end"] - units[index]["start"])
        selected.update(range(max(0, center - 1), min(len(units), center + 2)))
    return [units[index] for index in sorted(selected)]


def _json_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    excluded = ("method", "score", "status", "selection", "recovery")
    separately_rendered = {
        "all_candidates",
        "focal_candidates",
        "competing_candidate_evidence",
        "competing_action_evidence",
        "diff_span_evidence_json",
    }
    return {
        key: value
        for key, value in row.items()
        if value not in (None, "")
        and key not in separately_rendered
        and any(
            token in key.lower()
            for token in (
                "evidence",
                "reason",
                "discussiontools",
                "token",
                "assignment",
                "lifecycle",
                "actor",
                "speaker",
                "signature",
                "heading",
                "action",
            )
        )
        and not any(token in key.lower() for token in excluded)
    }


def _window_focal_ranges(row: Mapping[str, Any], raw: str) -> list[RawRange]:
    """Return candidate ranges intended to drive context windows."""

    reduced = dict(row)
    reduced.pop("competing_candidate_evidence", None)
    return [(item["start"], item["end"]) for item in _candidate_records(reduced, raw)]


def _changed_focal_ranges(changed: Iterable[RawRange], *, edge: int = 240) -> list[RawRange]:
    """Use both edges of huge diff spans without copying an entire rewritten page."""

    output: list[RawRange] = []
    for start, end in changed:
        if end - start <= 2 * edge:
            output.append((start, end))
        else:
            output.extend(((start, start + edge), (end - edge, end)))
    return output


def _changed_span_record(
    raw: str, start: int, end: int, *, raw_limit: int = 4096
) -> dict[str, Any]:
    result: dict[str, Any] = {"start": start, "end": end, "length": end - start}
    if end - start <= raw_limit:
        result["raw_text"] = raw[start:end]
    else:
        result["raw_text_omitted"] = True
        result["windowing"] = "both span edges are represented in explicitly offset windows"
    return result


def format_review_object(row: Mapping[str, Any]) -> dict[str, Any]:
    """Format a blinded, raw-evidence JSONL object from one joined row."""
    target = _text(_first(row, "target_wikitext", "target_raw_text", "target_text"))
    predecessor = _text(
        _first(row, "predecessor_wikitext", "predecessor_raw_text", "predecessor_text")
    )
    candidates = _candidate_records(row, target)
    changed = parse_ranges(
        _first(row, "action_target_changed_ranges_json", "target_changed_ranges_json"),
        length=len(target),
    )
    source = _text(_first(row, "source_text", "trusted_text", "anchor_text"))
    source_ranges = (
        [(match.start(), match.end()) for match in re.finditer(re.escape(source), target)]
        if source
        else []
    )
    focal = _window_focal_ranges(row, target)
    focal.extend(_changed_focal_ranges(changed))
    focal.extend(source_ranges)
    target_windows = build_raw_windows(target, focal)
    validate_windows(target, focal, target_windows)
    predecessor_changed = parse_ranges(
        row.get("predecessor_changed_ranges_json"), length=len(predecessor)
    )
    identifiers = {
        key: value
        for key, value in row.items()
        if value not in (None, "")
        and (key.endswith("_uid") or "revision_id" in key or key in {"page_id", "audit_uid"})
    }
    sample = {
        key: row[key]
        for key in (
            "primary_stratum",
            "design_cell",
            "inclusion_probability",
            "survey_weight",
            "population_n",
            "sample_n",
        )
        if key in row
    }
    return {
        "identifiers": identifiers,
        "sample_design": sample,
        "source_text": source,
        "target_windows": target_windows,
        "predecessor_windows": build_raw_windows(
            predecessor, _changed_focal_ranges(predecessor_changed)
        ),
        "neighboring_structural_units": _neighboring_units(target, focal),
        "candidates": candidates,
        "target_changed_spans": [
            _changed_span_record(target, start, end) for start, end in changed
        ],
        "predecessor_changed_spans": [
            _changed_span_record(predecessor, start, end) for start, end in predecessor_changed
        ],
        "competing_candidates": _competing_candidate_refs(row),
        "competing_actions": _first(row, "competing_action_evidence", "competing_actions_json")
        or [],
        "review_context": {
            key: row[key]
            for key in ("failure_reasons", "reason_codes_json")
            if row.get(key) not in (None, "")
        },
        "evidence": _json_evidence(row),
    }
