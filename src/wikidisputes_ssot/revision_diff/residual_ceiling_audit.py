"""Resumable, blinded-at-display audit packets for residual-ceiling review.

The CSV is the authoritative label store.  Every call to :func:`label_audit_row`
rewrites it atomically, so reviewers can stop after any row without losing a
completed label.  The companion HTML is deliberately display-only: it omits
method labels, statuses, and scores while retaining the raw evidence needed to
make a judgment.
"""

from __future__ import annotations

import csv
import html
import io
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from wikidisputes_ssot.io import atomic_write_bytes

RECOVERABILITY_VALUES = frozenset(
    {
        "existing_evidence_exact",
        "deterministic_rule_possible",
        "human_only",
        "ambiguous",
        "source_action_mismatch",
        "no_identifiable_comment",
    }
)
CANDIDATE_ERROR_VALUES = frozenset({"exact", "overwide", "underwide", "wrong", "none"})
CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})

LABEL_COLUMNS = (
    "recoverability",
    "chosen_candidate",
    "manual_raw_start",
    "manual_raw_end",
    "candidate_error",
    "rule_family",
    "confidence",
    "evidence_note",
)

# These columns are for the machine-readable sampling/summary layer.  HTML
# intentionally does not show them, so they do not bias the human reviewer.
HIDDEN_COLUMNS = frozenset(
    {
        "b_status",
        "method_b_status",
        "method_a_status",
        "status",
        "score",
        "scores",
        "candidate_score",
        "candidate_confidence",
        "primary_stratum",
        "inclusion_probability",
        "survey_weight",
        "stratum_population_n",
        "stratum_sample_n",
    }
)

NORMALIZED_INPUT_COLUMNS = frozenset(
    {
        "target_wikitext",
        "target_raw_text",
        "predecessor_wikitext",
        "predecessor_raw_text",
        "target_text",
        "diff_operations_json",
        "target_changed_ranges_json",
        "predecessor_changed_ranges_json",
        "action_target_changed_ranges_json",
        "hunk_attribution_evidence_json",
        "candidate_raw",
        "candidate_body",
        "candidate_raw_body",
        "method_b_candidate_raw_body",
        "candidate_spans_json",
        "competing_candidates_json",
        "competing_actions_json",
        "boundary_evidence_json",
    }
)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _records(rows: Any) -> list[dict[str, Any]]:
    """Accept pandas-like frames as well as a regular iterable of mappings."""
    if hasattr(rows, "to_dict"):
        return [dict(row) for row in rows.to_dict("records")]
    return [dict(row) for row in rows]


def _first(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _json(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _excerpt(value: Any, start: Any = None, end: Any = None, limit: int = 900) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        span_start = max(0, min(len(text), int(start)))
        span_end = max(span_start, min(len(text), int(end)))
    except (TypeError, ValueError):
        return text[:limit] + ("…" if len(text) > limit else "")
    span_length = span_end - span_start
    if span_length >= limit:
        half = max(1, (limit - 1) // 2)
        return text[span_start : span_start + half] + "…" + text[span_end - half : span_end]
    context = limit - span_length
    left = max(0, span_start - context // 2)
    right = min(len(text), span_end + context - (span_start - left))
    left = max(0, left - (limit - (right - left)))
    prefix = "…" if left else ""
    suffix = "…" if right < len(text) else ""
    return prefix + text[left:right] + suffix


def _evidence(row: Mapping[str, Any], excerpt_limit: int) -> dict[str, str]:
    """Normalize heterogeneous workflow evidence into stable reviewer fields."""
    target = _first(row, "target_wikitext", "target_text", "target_raw_text")
    predecessor = _first(row, "predecessor_wikitext", "predecessor_text", "predecessor_raw_text")
    target_start = _first(row, "target_changed_start", "target_start")
    target_end = _first(row, "target_changed_end", "target_end")
    previous_start = _first(row, "predecessor_changed_start", "predecessor_start")
    previous_end = _first(row, "predecessor_changed_end", "predecessor_end")
    discussiontools = (
        {
            key: value
            for key, value in row.items()
            if "discussiontools" in key.lower() and value not in (None, "")
        }
        if row.get("discussiontools_evidence") is True
        else {}
    )
    return {
        "source_text": _text(_first(row, "source_text", "trusted_text", "anchor_text")),
        "target_raw_revision_excerpt": _excerpt(target, target_start, target_end, excerpt_limit),
        "predecessor_raw_revision_excerpt": _excerpt(
            predecessor, previous_start, previous_end, excerpt_limit
        ),
        "diff_action_spans": _json(
            {
                key: row.get(key)
                for key in (
                    "action_type",
                    "action_class",
                    "lifecycle",
                    "changed_ranges_json",
                    "target_changed_ranges_json",
                    "predecessor_changed_ranges_json",
                    "action_target_changed_ranges_json",
                    "hunk_attribution_evidence_json",
                    "action_offset_hint",
                )
                if row.get(key) not in (None, "")
            }
        ),
        "existing_candidate_spans_text": _json(
            {
                key: row.get(key)
                for key in (
                    "candidate_body",
                    "candidate_raw_body",
                    "method_b_candidate_raw_body",
                    "candidate_start",
                    "candidate_end",
                    "body_start",
                    "body_end",
                    "candidate_spans_json",
                    "competing_candidates_json",
                    "competing_actions_json",
                    "boundary_evidence_json",
                )
                if row.get(key) not in (None, "")
            }
        ),
        "nearby_signatures_headings": _json(
            {
                key: row.get(key)
                for key in (
                    "signature_timestamp",
                    "signature_author",
                    "revision_actor",
                    "wikiconv_speaker",
                    "wikidisputes_speaker",
                    "nearby_signatures_json",
                    "nearby_headings_json",
                    "heading_path",
                )
                if row.get(key) not in (None, "")
            }
        ),
        "discussiontools_evidence": _json(discussiontools) if discussiontools else "",
    }


def _csv_bytes(rows: Iterable[Mapping[str, Any]], columns: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    csv.field_size_limit(sys.maxsize)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def build_residual_audit_packet(
    sampled_rows: Any,
    *,
    csv_path: Path,
    html_path: Path,
    metadata: Mapping[str, Any] | None = None,
    excerpt_limit: int = 900,
) -> dict[str, Any]:
    """Create a CSV label store and its blinded, static HTML reviewer view.

    ``sampled_rows`` should already include deterministic sampling fields such
    as ``audit_uid``, primary stratum, N/n, inclusion probability and weight.
    Calling this a second time refuses to overwrite existing labels.
    """
    if csv_path.exists():
        existing, _ = _read_csv(csv_path)
        if any(row.get("recoverability") for row in existing):
            raise ValueError(f"refusing to overwrite labeled audit packet: {csv_path}")
    packet: list[dict[str, Any]] = []
    for index, source in enumerate(_records(sampled_rows), start=1):
        uid = _text(_first(source, "audit_uid", "source_row_uid", "entity_uid", "id"))
        if not uid:
            uid = f"residual-ceiling-{index:04d}"
        packet.append(
            {
                "audit_uid": uid,
                "review_order": index,
                **{
                    key: source[key]
                    for key in source
                    if key not in {"audit_uid"}
                    and key not in LABEL_COLUMNS
                    and key not in NORMALIZED_INPUT_COLUMNS
                },
                **_evidence(source, excerpt_limit),
                **{column: "" for column in LABEL_COLUMNS},
            }
        )
    columns = list(dict.fromkeys(key for row in packet for key in row))
    atomic_write_bytes(csv_path, _csv_bytes(packet, columns))
    render_residual_audit_html(csv_path, html_path, metadata=metadata)
    return {
        "csv_path": str(csv_path),
        "html_path": str(html_path),
        "rows": len(packet),
        "metadata": dict(metadata or {}),
    }


def render_residual_audit_html(
    csv_path: Path, html_path: Path, *, metadata: Mapping[str, Any] | None = None
) -> None:
    """Render the current CSV to a display-only reviewer packet."""
    rows, _ = _read_csv(csv_path)
    parts = [
        "<!doctype html><meta charset=utf-8><title>Residual ceiling audit</title>",
        "<style>body{font:14px system-ui;max-width:1200px;margin:auto;padding:1rem}"
        "article{border:1px solid #bbb;margin:1rem 0;padding:1rem}pre{white-space:pre-wrap;"
        "word-break:break-word;background:#f6f6f6;padding:.7rem}summary{cursor:pointer}"
        ".done{border-left:6px solid #398439}</style>",
        "<h1>Residual ceiling audit</h1>",
        "<p>Display is blinded to method/status/scores. Labels are persisted with the "
        "label command, then refresh this file.</p>",
    ]
    if metadata:
        parts.append("<details><summary>Packet metadata</summary><pre>")
        parts.append(html.escape(json.dumps(dict(metadata), ensure_ascii=False, indent=2)))
        parts.append("</pre></details>")
    for row in rows:
        done = " done" if row.get("recoverability") else ""
        review_order = html.escape(row.get("review_order", ""))
        parts.append(f'<article class="{done.strip()}"><h2>Row {review_order}</h2>')
        for title, key in (
            ("Source/current text", "source_text"),
            ("Target raw revision excerpt", "target_raw_revision_excerpt"),
            ("Predecessor excerpt", "predecessor_raw_revision_excerpt"),
            ("Diff/action spans", "diff_action_spans"),
            ("Existing candidate spans/text", "existing_candidate_spans_text"),
            ("Nearby signatures/headings", "nearby_signatures_headings"),
            ("DiscussionTools evidence", "discussiontools_evidence"),
        ):
            value = row.get(key, "")
            if value:
                parts.append(
                    f"<details><summary>{title}</summary><pre>{html.escape(value)}</pre></details>"
                )
        if row.get("recoverability"):
            label = {column: row.get(column, "") for column in LABEL_COLUMNS}
            parts.append("<details open><summary>Saved label</summary><pre>")
            parts.append(html.escape(json.dumps(label, ensure_ascii=False, indent=2)))
            parts.append("</pre></details>")
        parts.append("</article>")
    atomic_write_bytes(html_path, "".join(parts).encode("utf-8"))


def label_audit_row(
    csv_path: Path,
    audit_uid: str,
    *,
    recoverability: str,
    chosen_candidate: str = "",
    manual_raw_start: int | str | None = None,
    manual_raw_end: int | str | None = None,
    candidate_error: str = "none",
    rule_family: str = "",
    confidence: str = "",
    evidence_note: str = "",
    html_path: Path | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Immediately and atomically persist one completed human label."""
    if recoverability not in RECOVERABILITY_VALUES:
        raise ValueError(f"invalid recoverability: {recoverability}")
    if candidate_error not in CANDIDATE_ERROR_VALUES:
        raise ValueError(f"invalid candidate_error: {candidate_error}")
    if confidence not in CONFIDENCE_VALUES:
        raise ValueError(f"invalid confidence: {confidence}")
    if not evidence_note.strip() or len(evidence_note) > 500:
        raise ValueError("evidence_note must contain 1 to 500 characters")
    if (manual_raw_start in (None, "")) != (manual_raw_end in (None, "")):
        raise ValueError("manual_raw_start and manual_raw_end must be supplied together")
    offsets: dict[str, int] = {}
    for name, value in (("manual_raw_start", manual_raw_start), ("manual_raw_end", manual_raw_end)):
        if value not in (None, ""):
            try:
                offsets[name] = int(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be an integer") from error
    if offsets and not 0 <= offsets["manual_raw_start"] < offsets["manual_raw_end"]:
        raise ValueError("manual raw offsets must satisfy 0 <= start < end")
    if recoverability in {
        "existing_evidence_exact",
        "deterministic_rule_possible",
        "human_only",
    } and not (chosen_candidate.strip() or offsets):
        raise ValueError("recoverable labels require a chosen candidate or manual raw offsets")
    if recoverability == "deterministic_rule_possible" and not rule_family.strip():
        raise ValueError("deterministic_rule_possible requires rule_family")
    rows, columns = _read_csv(csv_path)
    matches = [row for row in rows if row.get("audit_uid") == audit_uid]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one audit_uid {audit_uid!r}, found {len(matches)}")
    row = matches[0]
    row.update(
        {
            "recoverability": recoverability,
            "chosen_candidate": chosen_candidate,
            "manual_raw_start": _text(manual_raw_start),
            "manual_raw_end": _text(manual_raw_end),
            "candidate_error": candidate_error,
            "rule_family": rule_family,
            "confidence": confidence,
            "evidence_note": evidence_note,
        }
    )
    atomic_write_bytes(csv_path, _csv_bytes(rows, columns))
    if html_path is not None:
        render_residual_audit_html(csv_path, html_path, metadata=metadata)
    return {"audit_uid": audit_uid, "recoverability": recoverability, "csv_path": str(csv_path)}
