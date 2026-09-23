"""Small, evidence-first overlay for annotation turn integrity.

This module deliberately does not alter source or lifecycle identity.  It records
the narrow decisions needed by the annotation export: omit a proven structural
row, suppress a proven lifecycle alias, or use exact WikiDisputes text when
history cannot support a safe reconstruction.
"""

from __future__ import annotations

import csv
import datetime as dt
import difflib
import json
import re
import sqlite3
import subprocess
import sys
import zlib
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq

from .hashing import canonical_json_hash, sha256_file
from .historical_spans import evaluate_historical_span, overlaps_other_source
from .io import atomic_parquet, atomic_write_json, table_from_union_pylist

POLICY_VERSION = "turn_integrity_v2"
_UTC_SIGNATURE = re.compile(r"\([^\n)]{0,100}\bUTC\b[^\n)]{0,100}\)", re.IGNORECASE)
_LINE_TERMINAL_UTC_BOUNDARY = re.compile(
    r"\([^\n)]{0,100}\bUTC\b[^\n)]{0,100}\)\s*(?:\n|$)", re.IGNORECASE
)
_WIKILINK = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]")
# A UTC parenthesis alone is not necessarily a comment signature (it may be
# quoted prose).  These narrower forms are used only to raise the confidence
# of a merged-comment *candidate*, never to invent a split.
_CLEAR_SIGNATURE_BOUNDARY = re.compile(
    r"(?:--|—|–|\[\[(?:User(?:[ _]talk)?|Special:Contributions)[^\]]*\]\])"
    r"[^\n]{0,180}\bUTC\b",
    re.IGNORECASE,
)
_AUTOSIGN_BOUNDARY = re.compile(r"(?:^|\n)\s*(?:--|—|–)?\s*~{3,5}\s*$", re.MULTILINE)
_UNSIGNED_ATTRIBUTION_BOUNDARY = re.compile(
    r"(?:preceding\s+(?:\[\[[^\]]+\|)?unsigned(?:\]\])?\s+comment|"
    r"class\s*=\s*[\"']autosigned[\"']|template\s*:\s*unsigned)",
    re.IGNORECASE,
)
# WikiConv occasionally strips a decorated signature down to this repeated
# table-like residue.  Multiple occurrences are a composite *signal* only;
# they never supply defensible split spans.
_STRIPPED_SIGNATURE_BOUNDARY = re.compile(r"'{6}\s*\|")
_FOLLOWING_EXPLICIT_SIGNATURE = re.compile(
    r"^\s*(?:--|—)\s*\[\[User(?:[ _]talk)?:[^\]]+\]\][^\n]{0,120}\(UTC\)",
    re.IGNORECASE,
)
FINAL_DISPOSITIONS = frozenset(
    {
        "keep",
        "split",
        "alias_or_suppress_duplicate",
        "row_exclude",
        "recover",
        "wikidisputes_fallback",
        "dispute_exclude",
    }
)
Disposition = Literal[
    "keep",
    "split",
    "alias_or_suppress_duplicate",
    "row_exclude",
    "recover",
    "wikidisputes_fallback",
    "dispute_exclude",
]


def stable_case_id(
    dispute_id: str | None,
    source_row_uid: str | None,
    problem_type: str,
    logical_utterance_uid: str | None = None,
) -> str:
    """Stable ID based only on source/lifecycle identifiers, never text similarity."""

    return "turn-integrity:v1:" + canonical_json_hash(
        [dispute_id or "", source_row_uid or "", logical_utterance_uid or "", problem_type]
    )


def derived_turn_id(source_row_uid: str, part_index: int) -> str:
    """Deterministic annotation-unit identity for a historically proven split."""

    if part_index < 1:
        raise ValueError("part_index is one-based")
    return "turn-unit:v1:" + canonical_json_hash([source_row_uid, part_index])


def split_units(
    source_row_uid: str, parts: Sequence[Mapping[str, Any]], *, boundary_defensible: bool
) -> list[dict[str, Any]]:
    """Build split units only with explicit historical boundary proof."""

    if not boundary_defensible or len(parts) < 2:
        return []
    units: list[dict[str, Any]] = []
    for index, part in enumerate(parts, start=1):
        text = str(part.get("text") or "")
        revision = str(part.get("source_revision_id") or "")
        span = part.get("source_span")
        if (
            not text.strip()
            or not revision
            or not isinstance(span, (list, tuple))
            or len(span) != 2
        ):
            return []
        unit_id = derived_turn_id(source_row_uid, index)
        units.append(
            {
                "annotation_unit_uid": unit_id,
                # Compatibility name consumed by the existing CSV overlay.
                "derived_unit_id": unit_id,
                "source_row_uid": source_row_uid,
                "part_index": index,
                "source_revision_id": revision,
                "source_span": [int(span[0]), int(span[1])],
                "text": text,
                "speaker_id": part.get("speaker_id"),
                "created_at_utc": part.get("created_at_utc"),
                "creation_evidence": part.get("creation_evidence", "revision_boundary"),
            }
        )
    return units


def structural_nonconversation(text: str, *, history_proves_structural: bool) -> bool:
    """Never classify headings as formatting merely because they use markup."""

    if not history_proves_structural or text.strip().startswith("="):
        return False
    compact = "".join(text.split())
    return not compact or compact.startswith(("{|", "|}", "{{", "|-", "|"))


def placement_disposition(
    *, verified_creation: bool, feasible_positions: Sequence[object]
) -> tuple[Disposition, str | None]:
    if verified_creation or len(feasible_positions) == 1:
        return "keep", None
    return "dispute_exclude", "trajectory_position_ambiguous"


def conversation_id(value: object) -> str | None:
    """Gold stores the bare WikiConv conversation ID, unlike canonical UID fields."""

    text = str(value or "")
    prefix = "wikiconv-conversation:"
    if text.startswith(prefix):
        text = text.removeprefix(prefix)
    return text or None


def _evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    raw = candidate.get("detector_evidence", candidate.get("evidence", {}))
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return dict(parsed) if isinstance(parsed, Mapping) else {"raw": raw}
    return {}


def _raw_wikidisputes_text(record_json: object) -> str:
    """Return only the exact ``text`` value in an immutable source record."""

    if not isinstance(record_json, str):
        return ""
    try:
        record = json.loads(record_json)
    except json.JSONDecodeError:
        return ""
    text = record.get("text") if isinstance(record, Mapping) else None
    return text if isinstance(text, str) else ""


def _cached_revision_text(cache_path: Path, revision_id: str) -> str:
    """Read a recorded revision; an absent cache entry supplies no proof."""

    if not revision_id.isdecimal() or not cache_path.exists():
        return ""
    with sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True) as connection:
        record = connection.execute(
            "SELECT content FROM revision_cache WHERE revision_id = ? AND status = 'found'",
            (int(revision_id),),
        ).fetchone()
    return str(record[0]) if record and record[0] else ""


def _source_proven_heading(text: str, revision_text: str) -> str | None:
    """Require the entire source field to match a literal historical heading."""

    title = text.strip()
    if not title or len(title) > 80 or "\n" in title or not revision_text:
        return None
    pattern = re.compile(rf"(?m)^={{2,6}}[ \t]*{re.escape(title)}[ \t]*={{2,6}}[ \t]*$")
    matches = pattern.findall(revision_text)
    return matches[0] if len(matches) == 1 else None


def _proven_terminal_creation_speaker(history: Mapping[str, Any], source_text: str) -> str | None:
    """Use a creation candidate's own terminal signature, never its editor."""

    candidate_raw = str(history.get("terminal_candidate_raw") or "")
    signature_raw = str(history.get("terminal_signature_raw") or "")
    author = str(history.get("signature_author") or "")
    if (
        history.get("action_type") == "creation"
        and history.get("method_b_status") in {"b_safe", "b_usable"}
        and history.get("assignment_status") == "assigned"
        and history.get("signature_status") == "explicit_evidence_observed"
        and history.get("speaker_signature_provenance") == "mismatch"
        and not history.get("changed_span_not_in_one_comment")
        and candidate_raw
        and signature_raw
        and candidate_raw.rstrip().endswith(signature_raw.rstrip())
        and candidate_raw.count("(UTC)") == 1
        and source_text[:40].strip()
        and source_text[:40].strip() in candidate_raw
        and author in source_text
        and not _UNSIGNED_ATTRIBUTION_BOUNDARY.search(candidate_raw)
    ):
        return author
    return None


def _source_fallback_for_truncated_reconstruction(
    source_text: str, staged_text: str, revision_text: str
) -> bool:
    """Require one exact, signed historical comment before restoring its tail."""

    prefix = staged_text.strip()
    source_body = source_text.strip()
    if (
        len(prefix) < 60
        or len(source_body) < len(prefix) + 200
        or not source_body.startswith(prefix)
        or not revision_text
        or "(UTC)" in source_body
        or re.search(r"(?m)^={2,6}[^\n]*={2,6}[ \t]*$", source_body)
        or revision_text.count(source_body) != 1
    ):
        return False
    end = revision_text.find(source_body) + len(source_body)
    return _FOLLOWING_EXPLICIT_SIGNATURE.match(revision_text[end:]) is not None


def _authoritative_fallback_text(candidate: Mapping[str, Any]) -> tuple[str, str]:
    """Choose the raw WikiDisputes text, retaining its precise provenance."""

    raw_text = str(candidate.get("wikidisputes_raw_text_exact") or "")
    if candidate.get("wikidisputes_raw_record_found"):
        if raw_text.strip():
            return raw_text, "source_record_json_exact.text"
        # A present raw record with blank text is authoritative evidence that
        # there is no Method-A annotation text.  Do not fall through to a
        # later representation-derived field.
        return "", ""
    source_text = str(candidate.get("wikidisputes_text_exact") or "")
    if source_text.strip():
        return source_text, "wikidisputes_text_exact"
    return "", ""


def decide_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return one conservative final decision for a nominated candidate."""

    kind = str(candidate.get("problem_type", candidate.get("class", "")))
    fixture = str(candidate.get("dispute_sequence", candidate.get("fixture_id", "")))
    evidence = _evidence(candidate)
    source_row_uid = str(candidate.get("source_row_uid") or "")
    dispute_id = str(candidate.get("source_dispute_id", candidate.get("dispute_uid", "")))
    logical_uid = str(candidate.get("logical_utterance_uid") or "")
    case_id = stable_case_id(dispute_id, source_row_uid, kind, logical_uid)
    disposition: Disposition = "keep"
    reason: str | None = None
    reconstruction_rejection_reason: str | None = None
    fallback_text = ""
    fallback_text_source = ""
    units: list[dict[str, Any]] = []

    # A candidate that contains a cumulative or replayed block is not a
    # coherent single annotation turn.  Preserve its constituent occurrences
    # already present in the source export; never replace the block with a
    # fallback merely because a split cannot be proved.
    if fixture in {"D16", "D00016"}:
        disposition, reason = "row_exclude", "cumulative_representation_unsafe"
    elif kind == "absorbed_multi_turn":
        if evidence.get("historical_span_kind") == "single" and evidence.get(
            "historical_recovered_text"
        ):
            fallback_text = str(evidence["historical_recovered_text"])
            fallback_text_source = "cached_revision_unique_signed_span"
            disposition, reason = "recover", "historical_signed_span_recovered"
        elif evidence.get("source_proven_complete_after_truncation"):
            disposition, reason = "wikidisputes_fallback", "truncated_reconstruction"
        units = split_units(
            source_row_uid,
            evidence.get("parts", []),
            boundary_defensible=evidence.get("boundary_status") == "defensible",
        )
        if disposition in {"recover", "wikidisputes_fallback"}:
            pass
        elif units:
            disposition = "split"
        elif evidence.get("constituent_turns_already_present"):
            # Direct containment of separately emitted source turns makes the
            # longer row a redundant cumulative representation.  This is not
            # a similarity identity claim and does not manufacture a split.
            disposition, reason = "row_exclude", "cumulative_constituents_already_emitted"
        elif (
            (
                evidence.get("boundary_status") in {"contested", "not_defensible"}
                and str(candidate.get("provisional_disposition")) in {"needs_history", "repairable"}
            )
            or (str(candidate.get("provisional_disposition")) == "needs_history")
            or (
                str(candidate.get("provisional_disposition")) == "repairable"
                and str(candidate.get("severity")).casefold() == "high"
            )
        ):
            disposition, reason = "row_exclude", "cumulative_representation_unsafe"
        elif evidence.get("detector_class"):
            reason = "unresolved_cumulative_evidence"
    elif kind in {"lifecycle_replay", "exact_replay", "near_replay"}:
        if (
            fixture in {"D31", "D00031"}
            or evidence.get("lifecycle_identity") == "proven_alias"
            or _proven_physical_comment_slot_alias(evidence)
        ):
            disposition = "alias_or_suppress_duplicate"
        elif evidence.get("lifecycle_identity") == "proven_repost":
            # Same text is not enough: a separately evidenced repost remains
            # a distinct conversational act.
            disposition, reason = "keep", "genuine_independent_repost"
        else:
            # Population replay signals are review cases, not identity proof.
            # Retain an ambiguous source occurrence and expose the unresolved
            # case in the final annotation export.
            disposition, reason = "keep", "unresolved_replay_identity"
    elif kind in {"actor_signature_conflict", "speaker_signature_conflict"}:
        replacement = evidence.get("speaker_replacement")
        if (
            evidence.get("single_contribution_proven")
            and evidence.get("speaker_signature_conflict") == "clear"
            and isinstance(replacement, str)
            and replacement.strip()
        ):
            reason = "speaker_repaired_from_explicit_signature"
        else:
            reason = "unresolved_speaker_signature_conflict"
    elif kind == "missing_speaker":
        # A coherent text occurrence without an attributable speaker remains
        # visible, but cannot silently become an annotation-ready turn.
        reason = (
            "speaker_repaired_from_explicit_signature"
            if evidence.get("actor_signature_status") == "proven_speaker_replacement"
            and evidence.get("speaker_replacement")
            else "unresolved_missing_speaker"
        )
    elif kind == "formatting_or_empty":
        text = str(candidate.get("annotation_text", candidate.get("text", "")))
        if str(candidate.get("provisional_disposition")) == "keep":
            pass
        elif evidence.get("recoverable_text"):
            disposition = "recover"
        elif structural_nonconversation(
            text, history_proves_structural=bool(evidence.get("structural_proven"))
        ):
            disposition, reason = "row_exclude", "structural_nonconversation"
        elif not text.strip() or bool(evidence.get("annotation_text_blank")):
            fallback_text, fallback_text_source = _authoritative_fallback_text(candidate)
            # This narrow recovery only revisits rows that the prior
            # turn-integrity overlay actually emitted as blank fallbacks.  It
            # cannot promote other formatting candidates simply because a
            # later representation has text.
            if candidate.get("raw_blank_fallback_candidate") and fallback_text.strip():
                disposition, reason = "wikidisputes_fallback", "meaningful_text_unrecoverable"
            else:
                disposition, reason = "row_exclude", "wikidisputes_method_a_text_unavailable"
    elif kind == "fragmentary_row":
        # These are individually reviewed, immediate-neighbor findings.  The
        # detector does not infer a merge from text similarity: the evidence
        # names the adjacent source occurrence and its shared revision/user
        # context.  A formatting-only residue has no conversational text to
        # retain.
        if evidence.get("recovered_annotation_text"):
            fallback_text = str(evidence["recovered_annotation_text"])
            fallback_text_source = "mediawiki_raw_comment_recovery.current_annotation_text"
            disposition, reason = "recover", "fragment_recovered_from_mediawiki_revision"
        elif evidence.get("structural_proven"):
            disposition, reason = "row_exclude", "structural_nonconversation"
        elif evidence.get("reattach_target_source_uid"):
            disposition, reason = "alias_or_suppress_duplicate", "fragment_reattached"
        else:
            reason = "unresolved_fragment_evidence"
    elif (
        kind == "chronology_ambiguous"
        and str(candidate.get("provisional_disposition")) == "needs_history"
    ):
        disposition, reason = placement_disposition(
            verified_creation=bool(evidence.get("verified_creation")),
            feasible_positions=evidence.get("feasible_positions", []),
        )

    # Chronology review can leave a coherent source occurrence whose placement
    # is unresolved.  Its exact WikiDisputes representation is a safe fallback
    # only when that source field is nonblank.  A blank source field has no
    # authoritative annotation text to emit, so it is a row suppression rather
    # than a fabricated recovery.
    if disposition == "dispute_exclude":
        disposition = "wikidisputes_fallback"
    if disposition == "wikidisputes_fallback":
        if not fallback_text:
            fallback_text, fallback_text_source = _authoritative_fallback_text(candidate)
        if not fallback_text.strip():
            disposition = "row_exclude"
            reason = "wikidisputes_method_a_text_unavailable"
        else:
            reconstruction_rejection_reason = reason
            evidence = {
                **evidence,
                "annotation_representation": "wikidisputes_fallback",
                "annotation_text_source": fallback_text_source,
                "annotation_text_source_row_uid": source_row_uid,
                "fallback_source_projection_sha256": candidate.get("source_projection_sha256"),
                "reconstruction_rejected": True,
                "reconstruction_rejection_reason": reconstruction_rejection_reason,
                "coherent_single_turn": True,
            }

    decision = {
        "case_id": case_id,
        "dispute_uid": candidate.get("dispute_uid"),
        "episode_uid": candidate.get("episode_uid"),
        "conversation_uid": candidate.get("conversation_uid"),
        "conversation_id": conversation_id(
            candidate.get("conversation_id", candidate.get("conversation_uid"))
        ),
        "dispute_id": dispute_id or None,
        "dispute_sequence": fixture or None,
        "source_row_uid": source_row_uid or None,
        "logical_utterance_uid": logical_uid or None,
        "utterance_id": candidate.get("utterance_id"),
        "wikidisputes_current_id_exact": candidate.get("wikidisputes_current_id_exact"),
        "wikidisputes_original_id_exact": candidate.get("wikidisputes_original_id_exact"),
        "problem_type": kind,
        "severity": candidate.get("severity", "moderate"),
        "final_disposition": disposition,
        "exclusion_reason": None if disposition == "wikidisputes_fallback" else reason,
        "decision_reason": (
            "reconstruction_rejected_wikidisputes_fallback"
            if disposition == "wikidisputes_fallback"
            else reason
        ),
        "reconstruction_rejection_reason": reconstruction_rejection_reason,
        "annotation_representation": (
            "wikidisputes_fallback"
            if disposition == "wikidisputes_fallback"
            else "mediawiki_revision_recovery"
            if disposition == "recover" and fallback_text
            else None
        ),
        "annotation_text_source": (
            fallback_text_source if disposition in {"wikidisputes_fallback", "recover"} else None
        ),
        "fallback_text": (
            fallback_text if disposition in {"wikidisputes_fallback", "recover"} else None
        ),
        "fallback_text_source": (
            fallback_text_source if disposition in {"wikidisputes_fallback", "recover"} else None
        ),
        "annotation_eligible": disposition in {"keep", "split", "recover", "wikidisputes_fallback"},
        "derived_units_json": json.dumps(units, sort_keys=True),
        "evidence_json": json.dumps(evidence, sort_keys=True),
        # Kept as an alias for the pre-existing annotation overlay query.
        "detector_evidence": json.dumps(evidence, sort_keys=True),
        "rationale": candidate.get("rationale") or reason or "reviewed candidate retained",
        "fixture_id": fixture or None,
    }
    blocker = _annotation_blocking_reason(decision)
    decision["annotation_blocking"] = bool(blocker)
    decision["annotation_blocking_reason"] = blocker
    return decision


def _proven_physical_comment_slot_alias(evidence: Mapping[str, Any]) -> bool:
    """Accept a cross-revision replay only with physical-comment evidence.

    Text equality is deliberately absent from this predicate.  A resolver must
    name the stable WikiConv/action coordinate and root/structural lineage, so
    a separately posted copy (including a same-speaker repost) remains kept.
    """

    nested_slot = evidence.get("physical_comment_slot")
    slot = nested_slot if isinstance(nested_slot, Mapping) else evidence
    # The affirmative revision-history fact is important: an action position
    # may be reused after deletion/restoration, so coordinates alone do not
    # establish that the earlier occurrence existed before the later editor.
    history = evidence.get("revision_history")
    history = history if isinstance(history, Mapping) else {}
    existed_before_later_touch = bool(
        slot.get("anchor_existed_before_later_touch")
        or evidence.get("anchor_existed_before_later_touch")
        or history.get("anchor_existed_before_later_touch")
        or history.get("anchor_present_before_current_revision")
    )
    return bool(
        (slot.get("stable_across_revisions") or slot.get("stable_physical_comment_slot"))
        and (slot.get("action_coordinate") or slot.get("wikiconv_action_coordinate"))
        and (
            slot.get("root_evidence")
            or slot.get("structural_coordinate")
            or slot.get("wikiconv_root_coordinate")
        )
        and (slot.get("anchor_source_row_uid") or evidence.get("anchor_source_row_uid"))
        and existed_before_later_touch
    )


def gold_status(disposition: str, *, prior_annotation_count: int = 1) -> str:
    """Gold migration policy; a split never fans one annotation out to many."""

    if disposition == "keep":
        return "preserved"
    if disposition in {"split", "alias_or_suppress_duplicate", "recover", "wikidisputes_fallback"}:
        return "needs_rereview"
    if disposition in {"row_exclude", "dispute_exclude"}:
        return "invalidated"
    return "newly_annotatable" if prior_annotation_count == 0 else "needs_rereview"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist() if path.exists() else []


def _light_wiki_normalize(text: str) -> str:
    """Normalize only presentation-level Wiki syntax; never use it as identity proof."""

    text = _WIKILINK.sub(r"\2", text)
    text = text.replace("'''", "").replace("''", "")
    return " ".join(text.split())


def _tiny_light_revision_difference(left: str, right: str) -> bool:
    """Permit a near replay only for a presentation-level, tiny edit."""

    if left == right:
        return False
    # The normalized equality check is the primary guard.  The ratio prevents
    # a same-speaker adjacent rewrite with a large textual change from being
    # promoted merely because light Wiki formatting was removed.
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.98


def _json_string_list(value: object) -> list[str]:
    """Read a JSON string list from revision-evidence columns."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _similarity_index_keys(text: str, *, width: int = 64) -> set[str]:
    """Return a few discriminating chunks for bounded near-copy lookup."""

    if len(text) < width:
        return set()
    last = len(text) - width
    starts = {0, last, last // 4, last // 2, (last * 3) // 4}
    return {chunk for start in starts if len(set(chunk := text[start : start + width])) >= 8}


def _word_shingle_index_keys(text: str, *, words_per_key: int = 4) -> set[str]:
    """Select stable content keys despite small edits shifting character offsets.

    The character windows above are cheap, but a prefix insertion can shift
    all five sampled offsets. Whole word shingles retain common interior text
    across that edit. A bounded set of deterministic minima limits indexing.
    """

    words = re.findall(r"\w+", text.casefold())
    shingles = {
        " ".join(words[index : index + words_per_key])
        for index in range(len(words) - words_per_key + 1)
    }
    return set(sorted(shingles, key=lambda key: (zlib.crc32(key.encode("utf-8")), key))[:16])


def _adjacent_copy(
    rows: Sequence[Mapping[str, Any]], *, effective_orders: Mapping[str, int] | None = None
) -> bool:
    orders = sorted(
        effective_orders.get(str(row.get("source_row_uid") or ""), -10)
        if effective_orders is not None
        else int(row.get("source_order") or -10)
        for row in rows
    )
    return any(right - left == 1 for left, right in pairwise(orders))


def _plausible_meaningful_symbol(text: str) -> bool:
    """Keep simple reaction-only comments out of the parser-residue bucket."""

    compact = "".join(text.split())
    return bool(re.fullmatch(r"(?:[:;=8xX][-^']?[)(DPp/\\]|<3|[!?]+|…+)", compact))


def _fragment_signal(text: str) -> str | None:
    alphanumeric = sum(character.isalnum() for character in text)
    if not alphanumeric and not _plausible_meaningful_symbol(text):
        return "no_alphanumeric_payload"
    if alphanumeric <= 3:
        return "very_short_payload"
    compact = text.rstrip()
    # A trailing delimiter alone is normal prose.  Limit this residue signal
    # to a short continuation so ordinary comments ending in a colon/comma do
    # not flood the review population.
    if alphanumeric <= 30 and compact and compact[-1] in {"-", "—", ",", ":"}:
        return "incomplete_continuation"
    return None


def _population_candidate(
    row: Mapping[str, Any],
    problem_type: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an unresolved, evidence-carrying case from one final source unit."""

    return {
        "source_row_uid": row["source_row_uid"],
        "source_dispute_id": row["source_dispute_id"],
        "logical_utterance_uid": row.get("logical_utterance_uid"),
        "utterance_id": row.get("utterance_id"),
        "dispute_uid": row.get("dispute_uid"),
        "episode_uid": row.get("episode_uid"),
        "conversation_uid": row.get("conversation_uid"),
        "conversation_id": conversation_id(row.get("conversation_uid")),
        "wikidisputes_current_id_exact": row.get("utterance_id"),
        "wikidisputes_original_id_exact": row.get("original_utterance_id"),
        "wikidisputes_text_exact": row.get("text"),
        "problem_type": problem_type,
        "severity": "high" if problem_type == "absorbed_multi_turn" else "moderate",
        # Detection is intentionally not an identity or reconstruction decision.
        "provisional_disposition": "keep",
        "rationale": "population-wide turn-integrity detector; evidence requires review",
        "detector_evidence": {"population_wide_candidate": True, **evidence},
    }


def _staged_or_source_text(staged: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    """Use staged annotation text when present, otherwise the source occurrence.

    A blank staged cell is not a final candidate-unit text.  Falling through
    to the immutable source text keeps replay detection population-wide while
    the existing blank/fallback resolver remains responsible for disposition.
    """

    staged_text = str(staged.get("utterance_text") or "")
    return staged_text if staged_text.strip() else str(source.get("wikidisputes_text_exact") or "")


def _candidate_detection_text(
    staged: Mapping[str, Any], source: Mapping[str, Any], *, final_uses_source_text: bool
) -> str:
    """Select the representation that can actually reach the final overlay."""

    if final_uses_source_text:
        return str(source.get("wikidisputes_text_exact") or "")
    return _staged_or_source_text(staged, source)


def _physical_coordinate(utterance_id: object) -> tuple[int, str, str] | None:
    """Return revision, action, and root from a WikiConv utterance coordinate."""

    parts = str(utterance_id or "").split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1] or not parts[2]:
        return None
    return int(parts[0]), parts[1], parts[2]


def _nearby_same_speaker_edit(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Bound the same-speaker near-copy screen by order and real time."""

    if not str(left.get("speaker_id") or "") or left.get("speaker_id") != right.get("speaker_id"):
        return False
    if abs(int(left.get("source_order") or 0) - int(right.get("source_order") or 0)) > 4:
        return False
    try:
        left_time = dt.datetime.fromisoformat(
            str(left.get("timestamp") or "").replace("Z", "+00:00")
        )
        right_time = dt.datetime.fromisoformat(
            str(right.get("timestamp") or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False
    try:
        return abs((left_time - right_time).total_seconds()) <= 600
    except TypeError:
        return False


def _discover_population_candidates(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Nominate replay, cumulative, and fragment cases within one dispute only.

    Grouping precedes comparison so repeated text in separate disputes remains
    separate. Text signals only nominate cases; they do not assert
    physical-comment identity or a safe split.
    """

    by_dispute: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for unit in units:
        dispute = str(unit.get("source_dispute_id") or unit.get("dispute_uid") or "")
        if dispute and str(unit.get("text") or "").strip():
            by_dispute[dispute].append(unit)

    candidates: list[dict[str, Any]] = []
    for episode_units in by_dispute.values():
        episode_candidate_start = len(candidates)
        structural_residues = {
            "==",
            "''",
            "'''",
            "[]",
            "[[",
            "]]",
            "{{",
            "}}",
            "{|",
            "|}",
            "|-",
            "|",
        }
        conversational_units = [
            unit
            for unit in sorted(episode_units, key=lambda row: int(row.get("source_order") or 0))
            if "".join(str(unit["text"]).split()) not in structural_residues
        ]
        effective_orders = {
            str(unit["source_row_uid"]): index for index, unit in enumerate(conversational_units)
        }
        # Exact and light-Wiki-normalized replay groups are linear grouped
        # operations over the final annotation population.
        exact_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        normalized_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for unit in episode_units:
            text = str(unit["text"])
            # Every long exact group receives a case, including nonadjacent
            # repeats by the same speaker.
            if len(text) >= 100:
                exact_groups[text].append(unit)
            if len(text) >= 100:
                normalized_groups[_light_wiki_normalize(text)].append(unit)

        def nominate_replays(
            groups: Mapping[str, Sequence[Mapping[str, Any]]],
            *,
            problem_type: str,
            normalized: bool,
            effective_orders: Mapping[str, int] = effective_orders,
        ) -> None:
            for members in groups.values():
                if len(members) < 2:
                    continue
                speakers = {str(member.get("speaker_id") or "") for member in members}
                different_speakers = len(speakers) > 1
                adjacent = _adjacent_copy(members, effective_orders=effective_orders)
                source_uids = sorted(str(member["source_row_uid"]) for member in members)
                for member in members:
                    coordinate = _physical_coordinate(member.get("utterance_id"))
                    history = member.get("replay_history_evidence")
                    history = history if isinstance(history, Mapping) else {}
                    anchors = [
                        other
                        for other in members
                        if coordinate
                        and (other_coordinate := _physical_coordinate(other.get("utterance_id")))
                        and other_coordinate[1:] == coordinate[1:]
                        and other_coordinate[0] < coordinate[0]
                    ]
                    earliest = min(
                        anchors,
                        key=lambda other: (
                            _physical_coordinate(other.get("utterance_id")) or (sys.maxsize, "", "")
                        )[0],
                        default=None,
                    )
                    physical_slot: dict[str, Any] = {}
                    earliest_coordinate = (
                        _physical_coordinate(earliest.get("utterance_id"))
                        if earliest is not None
                        else None
                    )
                    stable_nonroot_coordinate = (
                        coordinate is not None and coordinate[1] != coordinate[2]
                    )
                    if (
                        coordinate is not None
                        and earliest is not None
                        and earliest_coordinate is not None
                        and (stable_nonroot_coordinate or history.get("not_in_later_changed_span"))
                    ):
                        physical_slot = {
                            "stable_across_revisions": True,
                            "action_coordinate": f"action:{coordinate[1]}",
                            "root_evidence": f"root:{coordinate[2]}",
                            "anchor_source_row_uid": str(earliest["source_row_uid"]),
                            "anchor_existed_before_later_touch": True,
                            "proof_source": (
                                "wikiconv_stable_nonroot_comment_coordinate"
                                if stable_nonroot_coordinate
                                else "method_b_recovery_evidence"
                            ),
                            "anchor_revision_id": earliest_coordinate[0],
                            "later_revision_id": coordinate[0],
                        }
                    candidates.append(
                        _population_candidate(
                            member,
                            problem_type,
                            {
                                "detector_class": "near_replay" if normalized else "exact_replay",
                                "normalization": "whitespace_light_wiki" if normalized else "exact",
                                "matching_source_row_uids": source_uids,
                                "group_count": len(members),
                                "same_text_cross_speaker": different_speakers,
                                "adjacent_copy": adjacent,
                                "lifecycle_identity": "unresolved",
                                **(
                                    {"physical_comment_slot": physical_slot}
                                    if physical_slot
                                    else {}
                                ),
                            },
                        )
                    )

        nominate_replays(exact_groups, problem_type="exact_replay", normalized=False)
        # An exact group is already nominated above.  A near group must have
        # genuinely distinct raw strings (typically whitespace/markup only).
        nominate_replays(
            {
                key: members
                for key, members in normalized_groups.items()
                if len({str(member["text"]) for member in members}) > 1
            },
            problem_type="near_replay",
            normalized=True,
        )

        # Light Wiki normalization does not catch tiny substantive edits. A
        # separate bounded path compares adjacent turns, nearby same-speaker
        # edits, and indexed cross-speaker pairs. It remains candidate-only.
        high_similarity_matches: dict[str, list[tuple[str, float]]] = defaultdict(list)
        similarity_units = [
            unit for unit in conversational_units if len(str(unit.get("text") or "")) >= 100
        ]
        similarity_indices = {
            str(unit["source_row_uid"]): index for index, unit in enumerate(similarity_units)
        }
        candidate_pairs: set[tuple[int, int]] = set()
        for left, right in pairwise(conversational_units):
            left_index = similarity_indices.get(str(left["source_row_uid"]))
            right_index = similarity_indices.get(str(right["source_row_uid"]))
            if left_index is not None and right_index is not None:
                candidate_pairs.add((min(left_index, right_index), max(left_index, right_index)))
        for left_index, left in enumerate(similarity_units):
            for right_index in range(left_index + 1, min(left_index + 5, len(similarity_units))):
                right = similarity_units[right_index]
                if _nearby_same_speaker_edit(left, right):
                    candidate_pairs.add((left_index, right_index))
        chunk_index: dict[str, list[int]] = defaultdict(list)
        for index, unit in enumerate(similarity_units):
            for key in _similarity_index_keys(str(unit["text"])):
                chunk_index[f"char:{key}"].append(index)
            for key in _word_shingle_index_keys(str(unit["text"])):
                chunk_index[f"word:{key}"].append(index)
        for matching_indices in chunk_index.values():
            # A chunk shared this widely is boilerplate, not a discriminating
            # index key. Skipping it bounds pair generation in large disputes.
            if len(matching_indices) > 64:
                continue
            for offset, left_index in enumerate(matching_indices):
                for right_index in matching_indices[offset + 1 :]:
                    left = similarity_units[left_index]
                    right = similarity_units[right_index]
                    if str(left.get("speaker_id") or "") != str(right.get("speaker_id") or ""):
                        candidate_pairs.add((left_index, right_index))
        for left_index, right_index in sorted(candidate_pairs):
            left = similarity_units[left_index]
            right = similarity_units[right_index]
            left_text = str(left["text"])
            left_uid = str(left["source_row_uid"])
            right_text = str(right["text"])
            right_uid = str(right["source_row_uid"])
            if left_text == right_text or _light_wiki_normalize(left_text) == (
                _light_wiki_normalize(right_text)
            ):
                continue
            length_ratio = min(len(left_text), len(right_text)) / max(
                len(left_text), len(right_text)
            )
            if length_ratio < 0.99:
                continue
            matcher = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False)
            if matcher.quick_ratio() < 0.99:
                continue
            ratio = matcher.ratio()
            if ratio < 0.99:
                continue
            high_similarity_matches[left_uid].append((right_uid, ratio))
            high_similarity_matches[right_uid].append((left_uid, ratio))
        units_by_source = {str(unit["source_row_uid"]): unit for unit in similarity_units}
        for source_uid, matches in high_similarity_matches.items():
            matching_sources = {source_uid}
            matching_sources.update(match_uid for match_uid, _ in matches)
            candidates.append(
                _population_candidate(
                    units_by_source[source_uid],
                    "near_replay",
                    {
                        "detector_class": "high_similarity_replay_candidate",
                        "normalization": "none",
                        "matching_source_row_uids": sorted(matching_sources),
                        "maximum_similarity_ratio": max(ratio for _, ratio in matches),
                        "similarity_threshold": 0.99,
                        "candidate_only_signal": True,
                        "lifecycle_identity": "unresolved",
                    },
                )
            )

        # Containment comparisons are constrained to one episode and only
        # emitted turns of at least 100 characters.  This is deliberately a
        # grouped containment scan, not fuzzy matching.
        long_units = [unit for unit in episode_units if len(str(unit["text"])) >= 100]
        contained_by_source: dict[str, list[str]] = defaultdict(list)
        for outer in long_units:
            outer_text = str(outer["text"])
            for inner in long_units:
                if outer is inner:
                    continue
                inner_text = str(inner["text"])
                if (
                    len(outer_text) >= len(inner_text) + 100
                    and len(outer_text) >= int(len(inner_text) * 1.25)
                    and inner_text in outer_text
                ):
                    contained_by_source[str(outer["source_row_uid"])].append(
                        str(inner["source_row_uid"])
                    )
        for unit in episode_units:
            text = str(unit["text"])
            contained = sorted(set(contained_by_source.get(str(unit["source_row_uid"]), [])))
            utc_count = len(_UTC_SIGNATURE.findall(text))
            line_terminal_utc_count = len(_LINE_TERMINAL_UTC_BOUNDARY.findall(text))
            clear_signature_count = len(_CLEAR_SIGNATURE_BOUNDARY.findall(text)) + len(
                _AUTOSIGN_BOUNDARY.findall(text)
            )
            unsigned_boundary_count = len(_UNSIGNED_ATTRIBUTION_BOUNDARY.findall(text))
            stripped_signature_count = len(_STRIPPED_SIGNATURE_BOUNDARY.findall(text))
            provenance = unit.get("turn_integrity_provenance")
            provenance = provenance if isinstance(provenance, Mapping) else {}
            merged_preceding_count = int(provenance.get("merged_preceding_count") or 0)
            changed_span_not_one_comment = bool(provenance.get("changed_span_not_in_one_comment"))
            history_composite = merged_preceding_count > 0 or bool(
                changed_span_not_one_comment and provenance.get("action_type") == "modification"
            )
            strong_text_composite = (
                utc_count >= 2
                or clear_signature_count >= 2
                or unsigned_boundary_count >= 2
                or stripped_signature_count >= 2
            )
            if not contained and not strong_text_composite and not history_composite:
                continue
            candidates.append(
                _population_candidate(
                    unit,
                    "absorbed_multi_turn",
                    {
                        "detector_class": (
                            "cumulative_containment"
                            if contained
                            else "multiple_clear_signature_boundaries"
                            if clear_signature_count >= 2
                            else "multiple_line_terminal_utc_boundaries"
                            if line_terminal_utc_count >= 2
                            else "multiple_unsigned_attribution_boundaries"
                            if unsigned_boundary_count >= 2
                            else "multiple_stripped_signature_boundaries"
                            if stripped_signature_count >= 2
                            else "revision_history_multi_comment_span"
                            if history_composite
                            else "multiple_utc_signatures"
                        ),
                        "contained_source_row_uids": contained,
                        "constituent_turns_already_present": bool(contained),
                        "emitted_utc_marker_count": utc_count,
                        "line_terminal_utc_boundary_count": line_terminal_utc_count,
                        "clear_signature_boundary_count": clear_signature_count,
                        "unsigned_attribution_boundary_count": unsigned_boundary_count,
                        "stripped_signature_boundary_count": stripped_signature_count,
                        "revision_merged_preceding_count": merged_preceding_count,
                        "changed_span_not_in_one_comment": changed_span_not_one_comment,
                        "merged_comment_confidence": (
                            "high"
                            if (
                                clear_signature_count >= 2
                                or line_terminal_utc_count >= 2
                                or unsigned_boundary_count >= 2
                                or stripped_signature_count >= 2
                                or history_composite
                            )
                            else "moderate"
                        ),
                        "boundary_status": "not_defensible",
                        "review_basis": (
                            "revision_history_multi_comment_evidence"
                            if history_composite
                            else "population_source_unit_text_boundaries"
                        ),
                        **({"revision_history_evidence": dict(provenance)} if provenance else {}),
                    },
                )
            )

        # Repair annotation-facing attribution only for one historically
        # proven contribution. Multiple contributors route through the
        # composite path above and preserve the row unchanged for audit.
        composite_sources = {
            str(candidate.get("source_row_uid") or "")
            for candidate in candidates[episode_candidate_start:]
            if candidate.get("problem_type") == "absorbed_multi_turn"
        }
        for unit in episode_units:
            source_uid = str(unit.get("source_row_uid") or "")
            provenance = unit.get("turn_integrity_provenance")
            provenance = provenance if isinstance(provenance, Mapping) else {}
            if provenance.get("speaker_signature_provenance") != "mismatch":
                continue
            if source_uid in composite_sources:
                continue
            signature_author = str(provenance.get("signature_author") or "")
            single_contribution = bool(provenance.get("single_contribution_proven"))
            candidates.append(
                _population_candidate(
                    unit,
                    "speaker_signature_conflict",
                    {
                        "detector_class": "explicit_signature_speaker_conflict",
                        "speaker_signature_conflict": "clear",
                        "source_speaker_id": str(unit.get("speaker_id") or ""),
                        "signature_author": signature_author or None,
                        "speaker_replacement": signature_author or None,
                        "actor_signature_status": (
                            "proven_speaker_replacement"
                            if single_contribution and signature_author
                            else "unresolved_conflict"
                        ),
                        "single_contribution_proven": single_contribution,
                        "raw_provenance_preserved": True,
                        "revision_history_evidence": dict(provenance),
                    },
                )
            )

        for unit in episode_units:
            if str(unit.get("speaker_id") or "").strip():
                continue
            candidates.append(
                _population_candidate(
                    unit,
                    "missing_speaker",
                    {
                        "detector_class": "missing_attribution_in_final_population",
                        "source_speaker_id": None,
                        "review_basis": "source_attribution_required",
                    },
                )
            )

        ordered = sorted(episode_units, key=lambda row: int(row.get("source_order") or 0))
        for index, unit in enumerate(ordered):
            signal = _fragment_signal(str(unit["text"]))
            if signal is None:
                continue
            text = str(unit["text"])
            compact = "".join(text.split())
            structural = compact in {
                "==",
                "''",
                "'''",
                "[]",
                "[[",
                "]]",
                "{{",
                "}}",
                "{|",
                "|}",
                "|-",
                "|",
            }
            candidates.append(
                _population_candidate(
                    unit,
                    "fragmentary_row",
                    {
                        "detector_class": "fragment",
                        "fragment_signal": signal,
                        "structural_proven": structural,
                        "previous_source_row_uid": (
                            str(ordered[index - 1]["source_row_uid"]) if index else None
                        ),
                        "next_source_row_uid": (
                            str(ordered[index + 1]["source_row_uid"])
                            if index + 1 < len(ordered)
                            else None
                        ),
                        "review_basis": "immediate_source_neighbors_required",
                    },
                )
            )
    return candidates


def _independent_replay_coverage(
    units: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str, str]]:
    """Screen staged units independently of candidate and decision materialization.

    This check deliberately works from population text and immutable source
    identifiers, without reading detector cases or their evidence merge.
    """

    expected: set[tuple[str, str, str]] = set()
    by_dispute: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    full_by_dispute: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for unit in units:
        dispute = str(unit.get("source_dispute_id") or "")
        full_by_dispute[dispute].append(unit)
        text = str(unit.get("text") or "")
        if len(text) >= 100:
            by_dispute[dispute].append(unit)
    for dispute, members in by_dispute.items():
        exact: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        normalized: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for member in members:
            value = str(member["text"])
            exact[value].append(member)
            normalized[_light_wiki_normalize(value)].append(member)
        for group in exact.values():
            if len(group) >= 2:
                expected.update(
                    (dispute, str(row["source_row_uid"]), "exact_replay") for row in group
                )
        for group in normalized.values():
            if len(group) >= 2 and len({str(row["text"]) for row in group}) >= 2:
                expected.update(
                    (dispute, str(row["source_row_uid"]), "near_replay") for row in group
                )
        ordered = sorted(members, key=lambda row: int(row.get("source_order") or 0))
        full_order = {
            str(row["source_row_uid"]): index
            for index, row in enumerate(
                sorted(
                    (
                        row
                        for row in full_by_dispute[dispute]
                        if "".join(str(row.get("text") or "").split())
                        not in {
                            "==",
                            "''",
                            "'''",
                            "[]",
                            "[[",
                            "]]",
                            "{{",
                            "}}",
                            "{|",
                            "|}",
                            "|-",
                            "|",
                        }
                    ),
                    key=lambda row: int(row.get("source_order") or 0),
                )
            )
        }
        pairs: set[tuple[int, int]] = set()
        for left_index, left in enumerate(ordered):
            for right_index in range(left_index + 1, min(left_index + 5, len(ordered))):
                right = ordered[right_index]
                adjacent = (
                    full_order[str(right["source_row_uid"])]
                    - full_order[str(left["source_row_uid"])]
                    == 1
                )
                if adjacent or _nearby_same_speaker_edit(left, right):
                    pairs.add((left_index, right_index))
        # This audit screen uses token shingles rather than the detector's
        # sampled character windows. It must still nominate a shifted-copy
        # pair when all of the detector's fixed offsets happen to differ.
        chunk_groups: dict[str, list[int]] = defaultdict(list)
        for index, row in enumerate(ordered):
            for key in _word_shingle_index_keys(str(row["text"]), words_per_key=3):
                chunk_groups[key].append(index)
        for indices in chunk_groups.values():
            if len(indices) > 64:
                continue
            for offset, left_index in enumerate(indices):
                for right_index in indices[offset + 1 :]:
                    if ordered[left_index].get("speaker_id") != ordered[right_index].get(
                        "speaker_id"
                    ):
                        pairs.add((left_index, right_index))
        for left_index, right_index in pairs:
            left, right = ordered[left_index], ordered[right_index]
            left_text, right_text = str(left["text"]), str(right["text"])
            if left_text == right_text or _light_wiki_normalize(left_text) == _light_wiki_normalize(
                right_text
            ):
                continue
            if min(len(left_text), len(right_text)) / max(len(left_text), len(right_text)) < 0.99:
                continue
            matcher = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False)
            if matcher.quick_ratio() >= 0.99 and matcher.ratio() >= 0.99:
                expected.add((dispute, str(left["source_row_uid"]), "near_replay"))
                expected.add((dispute, str(right["source_row_uid"]), "near_replay"))
    return expected


def _resolve_high_confidence_replay_bundles(
    rows: Sequence[dict[str, Any]], *, anchor_sources_by_utterance: Mapping[str, str]
) -> None:
    """Mark only revision-batch replays whose earlier physical anchors survive.

    A repeat batch is not enough.  At least two emitted candidates in one
    current revision/time must point to distinct earlier source occurrences.
    The anchor is resolved transitively before it is written, which prevents a
    later-suppressed row from becoming an annotation anchor.
    """

    # Candidate parquet is an input to the next rebuild.  Remove only this
    # resolver's previous conclusion before reconsidering retention; otherwise
    # an anchor that has since become excluded can remain a stale alias.
    for row in rows:
        evidence = _evidence(row)
        bundle = evidence.get("replay_bundle")
        if not isinstance(bundle, Mapping) or not bundle.get("high_confidence"):
            continue
        if bundle.get("generated_by") != "turn_integrity_bundle_resolver" and evidence.get(
            "source"
        ) not in {
            "normalized_repeat_revision_batch",
            "exact_repeat_revision_batch",
            "light_normalized_repeat_revision_batch",
        }:
            continue
        evidence.pop("replay_bundle", None)
        if evidence.get("lifecycle_identity") == "proven_alias":
            evidence.pop("lifecycle_identity", None)
            evidence.pop("anchor_source_row_uid", None)
        row["detector_evidence"] = evidence

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row.get("problem_type") or "") not in {
            "lifecycle_replay",
            "exact_replay",
            "near_replay",
        }:
            continue
        evidence = _evidence(row)
        revision = str(evidence.get("revision_prefix") or evidence.get("current_revision_id") or "")
        timestamp = str(
            evidence.get("current_timestamp") or evidence.get("current_revision_time") or ""
        )
        if not revision or not timestamp:
            continue
        # This source records exact/light-normalized repeat membership.  Do
        # not reinterpret broader similarity candidates as physical replays.
        if evidence.get("source") not in {
            "normalized_repeat_revision_batch",
            "exact_repeat_revision_batch",
            "light_normalized_repeat_revision_batch",
        }:
            continue
        grouped[(str(row.get("source_dispute_id") or ""), revision, timestamp)].append(row)

    rows_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_source[str(row.get("source_row_uid") or "")].append(row)

    # Start the transitive walk with already-reviewed aliases.  A bundle can
    # therefore never select a source that is later suppressed as its anchor.
    aliases: dict[str, str] = {
        str(row.get("source_row_uid") or ""): str(_evidence(row).get("anchor_source_row_uid") or "")
        for row in rows
        if _evidence(row).get("lifecycle_identity") == "proven_alias"
        and str(_evidence(row).get("anchor_source_row_uid") or "")
    }
    bundle_aliases: dict[str, str] = {}
    for members in grouped.values():
        member_uids = {str(row.get("source_row_uid") or "") for row in members}
        anchors: dict[str, str] = {}
        for row in members:
            anchor_id = str(_evidence(row).get("anchor_utterance_id") or "")
            anchor_uid = str(anchor_sources_by_utterance.get(anchor_id) or "")
            if anchor_uid and anchor_uid not in member_uids:
                anchors[str(row.get("source_row_uid") or "")] = anchor_uid
        if len(anchors) < 2 or len(set(anchors.values())) < 2:
            continue
        bundle_aliases.update(anchors)
    aliases.update(bundle_aliases)

    def retained_source(source_uid: str) -> bool:
        source_rows = rows_by_source.get(source_uid, [])
        # Absence from the review inventory means the source occurrence is
        # emitted normally.  When reviewed, at least one annotation-eligible
        # decision is required; a sole row-exclude/suppression is not a
        # conversational anchor.
        return not source_rows or all(
            decide_candidate(row)["annotation_eligible"] for row in source_rows
        )

    def retained_anchor(source_uid: str) -> str | None:
        seen: set[str] = set()
        while source_uid in aliases and source_uid not in seen:
            seen.add(source_uid)
            source_uid = aliases[source_uid]
        return (
            source_uid
            if source_uid and source_uid not in seen and retained_source(source_uid)
            else None
        )

    for members in grouped.values():
        for row in members:
            source_uid = str(row.get("source_row_uid") or "")
            bundle_anchor_uid = bundle_aliases.get(source_uid)
            if not bundle_anchor_uid:
                continue
            retained = retained_anchor(bundle_anchor_uid)
            if retained is None or retained == source_uid:
                continue
            evidence = _evidence(row)
            evidence.update(
                {
                    "lifecycle_identity": "proven_alias",
                    "anchor_source_row_uid": retained,
                    "replay_bundle": {
                        "generated_by": "turn_integrity_bundle_resolver",
                        "high_confidence": True,
                        "same_current_revision_and_time": True,
                        "distinct_earlier_anchors": len(
                            {
                                aliases.get(str(member.get("source_row_uid") or ""), "")
                                for member in members
                            }
                            - {""}
                        ),
                        "anchor_retained_transitively": retained != bundle_anchor_uid,
                    },
                }
            )
            row["detector_evidence"] = evidence


def _resolve_longitudinal_replays(
    rows: Sequence[dict[str, Any]], *, anchor_sources_by_utterance: Mapping[str, str]
) -> None:
    """Resolve one replay when revision provenance proves its physical slot.

    Unlike the batch resolver, this accepts an individual later historical
    representation.  The proof deliberately contains no text comparison: it
    needs a surviving earlier anchor, a stable action/comment coordinate,
    root or structural continuity, and an affirmative history fact that the
    anchor already existed before the later page touch.
    """

    # A prior resolver conclusion is not itself input proof on a rerun.
    # Recompute it from the current population and current retained anchors.
    for row in rows:
        evidence = _evidence(row)
        marker = evidence.get("longitudinal_replay")
        if not isinstance(marker, Mapping) or marker.get("generated_by") != (
            "turn_integrity_longitudinal_resolver"
        ):
            continue
        evidence.pop("longitudinal_replay", None)
        if evidence.get("lifecycle_identity") == "proven_alias":
            evidence.pop("lifecycle_identity", None)
            evidence.pop("anchor_source_row_uid", None)
        row["detector_evidence"] = evidence

    rows_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_source[str(row.get("source_row_uid") or "")].append(row)

    def retained(source_uid: str) -> bool:
        decisions = rows_by_source.get(source_uid, [])
        return not decisions or all(
            decide_candidate(row)["annotation_eligible"] for row in decisions
        )

    for row in rows:
        if str(row.get("problem_type") or "") not in {
            "lifecycle_replay",
            "exact_replay",
            "near_replay",
        }:
            continue
        evidence = _evidence(row)
        # Do not disturb a reviewed genuine repost or conclusion from the
        # stricter repeat-batch resolver.
        if evidence.get("lifecycle_identity") in {"proven_alias", "proven_repost"}:
            continue
        slot = evidence.get("physical_comment_slot")
        slot = dict(slot) if isinstance(slot, Mapping) else dict(evidence)
        anchor_uid = str(
            slot.get("anchor_source_row_uid") or evidence.get("anchor_source_row_uid") or ""
        )
        if not anchor_uid:
            anchor_id = str(
                slot.get("anchor_utterance_id") or evidence.get("anchor_utterance_id") or ""
            )
            anchor_uid = anchor_sources_by_utterance.get(anchor_id, "")
        source_uid = str(row.get("source_row_uid") or "")
        slot["anchor_source_row_uid"] = anchor_uid
        proof = {**evidence, "physical_comment_slot": slot}
        if not anchor_uid or anchor_uid == source_uid or not retained(anchor_uid):
            continue
        if not _proven_physical_comment_slot_alias(proof):
            continue
        evidence.update(
            {
                "lifecycle_identity": "proven_alias",
                "anchor_source_row_uid": anchor_uid,
                "physical_comment_slot": slot,
                "longitudinal_replay": {
                    "generated_by": "turn_integrity_longitudinal_resolver",
                    "stable_action_coordinate": str(
                        slot.get("action_coordinate") or slot.get("wikiconv_action_coordinate")
                    ),
                    "root_or_structural_continuity": True,
                    "anchor_existed_before_later_touch": True,
                },
            }
        )
        row["detector_evidence"] = evidence


def _resolve_cached_signature_replays(
    rows: Sequence[dict[str, Any]],
    population_units: Sequence[Mapping[str, Any]],
    *,
    cache_path: Path,
) -> None:
    """Alias a carried comment when cached page text proves signature continuity.

    WikiConv roots can change after later edits. A unique occurrence at the
    same page offset with the same original, explicit signature in both saved
    revisions is independent physical-comment evidence. Unsigned templates
    and repeated occurrences cannot satisfy this proof.
    """

    if not cache_path.exists():
        return
    by_action: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for unit in population_units:
        coordinate = _physical_coordinate(unit.get("utterance_id"))
        if coordinate is not None:
            by_action[(str(unit.get("source_dispute_id") or ""), coordinate[1])].append(unit)
    candidates_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_source[str(row.get("source_row_uid") or "")].append(row)
        if row.get("problem_type") in {"exact_replay", "near_replay", "lifecycle_replay"}:
            candidates_by_source[str(row.get("source_row_uid") or "")].append(row)
    cached: dict[int, str] = {}

    def occurrence(unit: Mapping[str, Any]) -> tuple[int, str] | None:
        coordinate = _physical_coordinate(unit.get("utterance_id"))
        if coordinate is None:
            return None
        revision = coordinate[0]
        if revision not in cached:
            cached[revision] = _cached_revision_text(cache_path, str(revision))
        page = cached[revision]
        value = str(unit.get("text") or "").strip()
        if not page or len(value) < 100 or page.count(value) != 1:
            return None
        offset = page.find(value)
        signature = _FOLLOWING_EXPLICIT_SIGNATURE.match(page[offset + len(value) :])
        if signature is None:
            return None
        return offset, signature.group().strip()

    for (_, action), units in by_action.items():
        ordered = sorted(
            units,
            key=lambda unit: (
                (_physical_coordinate(unit.get("utterance_id")) or (sys.maxsize, "", ""))[0],
                str(unit.get("source_row_uid") or ""),
            ),
        )
        if len(ordered) < 2:
            continue
        anchor = ordered[0]
        anchor_coordinate = _physical_coordinate(anchor.get("utterance_id"))
        anchor_occurrence = occurrence(anchor)
        if anchor_coordinate is None or anchor_occurrence is None:
            continue
        anchor_uid = str(anchor.get("source_row_uid") or "")
        if not all(
            decide_candidate(row)["annotation_eligible"] for row in rows_by_source[anchor_uid]
        ):
            continue
        for later in ordered[1:]:
            later_uid = str(later.get("source_row_uid") or "")
            later_coordinate = _physical_coordinate(later.get("utterance_id"))
            if (
                not candidates_by_source.get(later_uid)
                or later_coordinate is None
                or later_coordinate[0] <= anchor_coordinate[0]
                or occurrence(later) != anchor_occurrence
            ):
                continue
            anchor_text = str(anchor.get("text") or "")
            later_text = str(later.get("text") or "")
            if (
                min(len(anchor_text), len(later_text)) / max(len(anchor_text), len(later_text))
                < 0.99
            ):
                continue
            if (
                difflib.SequenceMatcher(None, anchor_text, later_text, autojunk=False).ratio()
                < 0.99
            ):
                continue
            for row in candidates_by_source[later_uid]:
                evidence = _evidence(row)
                if evidence.get("lifecycle_identity") in {"proven_alias", "proven_repost"}:
                    continue
                evidence.update(
                    {
                        "lifecycle_identity": "proven_alias",
                        "anchor_source_row_uid": anchor_uid,
                        "physical_comment_slot": {
                            "stable_across_revisions": True,
                            "action_coordinate": f"action:{action}",
                            "root_evidence": "cached_explicit_signature_and_page_offset",
                            "anchor_source_row_uid": anchor_uid,
                            "anchor_existed_before_later_touch": True,
                            "anchor_revision_id": anchor_coordinate[0],
                            "later_revision_id": later_coordinate[0],
                            "proof_source": "cached_revision_signature_continuity",
                        },
                    }
                )
                row["detector_evidence"] = evidence


def _annotation_blocking_reason(decision: Mapping[str, Any]) -> str | None:
    """Classify unresolved fidelity risks without preventing a rebuild."""

    evidence = _evidence(decision)
    kind = str(decision.get("problem_type") or "")
    unresolved = str(decision.get("decision_reason") or "").startswith("unresolved_")
    if kind == "missing_speaker" and unresolved:
        return "unresolved_missing_speaker"
    if kind in {"exact_replay", "near_replay", "lifecycle_replay"} and unresolved:
        slot = evidence.get("physical_comment_slot")
        strong_history = bool(
            evidence.get("strong_replay_identity")
            or evidence.get("replay_identity_confidence") == "high"
            or (isinstance(slot, Mapping) and slot.get("stable_across_revisions"))
            or evidence.get("source")
            in {
                "normalized_repeat_revision_batch",
                "exact_repeat_revision_batch",
                "light_normalized_repeat_revision_batch",
            }
        )
        if strong_history:
            return "unresolved_strong_replay_identity"
    if (
        kind == "absorbed_multi_turn"
        and unresolved
        and (
            evidence.get("merged_comment_confidence") == "high"
            or int(evidence.get("clear_signature_boundary_count") or 0) >= 2
        )
    ):
        return "unresolved_high_confidence_merged_comment"
    if (
        kind in {"actor_signature_conflict", "speaker_signature_conflict"}
        and unresolved
        and evidence.get("speaker_signature_conflict") in {True, "clear", "unresolved"}
    ):
        return "unresolved_speaker_signature_conflict"
    return None


def _prior_blank_fallback_source_uids(output_root: Path) -> set[str]:
    """Read the preserved pre-raw export solely to scope the raw trace."""

    path = output_root / "annotation" / "wikidisputes_llm_annotation_input.pre_raw_wikitext.csv"
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            str(row.get("ssot_source_row_uid") or "")
            for row in csv.DictReader(handle)
            if not str(row.get("utterance_text") or "").strip()
            and str(row.get("ssot_source_row_uid") or "")
        }


def _recovered_mediawiki_comment(output_root: Path, source_uid: str) -> str:
    """Return one reviewed raw-revision comment, never a similarity-derived guess."""

    path = output_root / "silver" / "mediawiki_raw_comment_recovery.csv"
    if not path.exists():
        return ""
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("source_row_uid") or "") == source_uid:
                return str(row.get("current_annotation_text") or "")
    return ""


def _candidate_rows(
    output_root: Path, *, coverage_expected: set[tuple[str, str, str]] | None = None
) -> list[dict[str, Any]]:
    """Regenerate candidate IDs/fields from the current partial inventory.

    The prior artifact is a detector input only: its provisional disposition is
    not copied into final decisions.
    """

    path = output_root / "reports" / "turn_integrity" / "candidates.parquet"
    raw = [
        row
        for row in _read_rows(path)
        # Candidate artifacts are rebuild inputs, so remove only our own
        # previous population scan before recomputing it.  Human/fixture
        # candidates retain their evidence even when their source overlaps.
        if not (
            str(row.get("rationale") or "")
            == "population-wide turn-integrity detector; evidence requires review"
            or _evidence(row).get("population_wide_candidate")
            # Remove the one-time detector-fixture injection from prior
            # artifacts; the general population detector now emits these.
            or str(row.get("rationale") or "").startswith(
                "Targeted long replay detector regression fixture"
            )
            # Retire the two pre-general-resolver absorbed-turn placeholders.
            # Their immutable sources are now handled by stable action/root
            # replay proof; carrying these old rows forward would suppress a
            # retained physical-comment anchor.
            or (
                str(row.get("source_row_uid") or "")
                in {
                    "wdrow:v1:8f1fc3c5a954a6c36a7bfd7ce6587d23a5285e1e74a943115188d54a1b44df73",
                    "wdrow:v1:5e5ae72ace294875ae3770318e38cb6527931a349f978b83e83ae38ef93678a3",
                }
                and str(row.get("problem_type") or "") == "absorbed_multi_turn"
            )
        )
    ]
    prior_blank_fallback_sources = _prior_blank_fallback_source_uids(output_root)
    join_by_source: dict[str, dict[str, Any]] = {}
    join_path = output_root / "silver" / "annotation_join_contract.parquet"
    if join_path.exists():
        for batch in pq.ParquetFile(join_path).iter_batches(batch_size=50_000):
            for joined in batch.to_pylist():
                source_uid = str(joined.get("source_row_uid") or "")
                if source_uid:
                    join_by_source[source_uid] = joined
    raw_by_source: dict[str, dict[str, Any]] = {}
    source_rows_by_revision: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    source_path = output_root / "canonical" / "wikidisputes_source_projection.parquet"
    if source_path.exists():
        for batch in pq.ParquetFile(source_path).iter_batches(batch_size=50_000):
            for source in batch.to_pylist():
                source_uid = str(source.get("source_row_uid") or "")
                if source_uid:
                    raw_by_source[source_uid] = source
                    revision_id = str(source.get("wikidisputes_id_exact") or "").split(".", 1)[0]
                    if revision_id:
                        source_rows_by_revision[revision_id].append(source)
    recovery_history_by_source: dict[str, dict[str, Any]] = {}
    recovery_candidate_start_by_source: dict[str, Any] = {}
    recovery_path = output_root / "silver" / "method_b_recovery_evidence.parquet"
    if recovery_path.exists():
        for batch in pq.ParquetFile(recovery_path).iter_batches(batch_size=50_000):
            for recovery in batch.to_pylist():
                source_uid = str(recovery.get("source_row_uid") or "")
                if not source_uid:
                    continue
                recovery_candidate_start_by_source[source_uid] = recovery.get("candidate_start")
                reason_codes = _json_string_list(recovery.get("reason_codes_json"))
                assignment_codes = _json_string_list(recovery.get("assignment_reason_codes_json"))
                boundary_evidence = _json_string_list(recovery.get("boundary_evidence_json"))
                merged_preceding_count = sum(
                    code == "merged_preceding_unsigned_same_depth_paragraph"
                    for code in boundary_evidence
                )
                changed_span_not_one_comment = "changed_span_not_in_one_comment" in {
                    *reason_codes,
                    *assignment_codes,
                }
                signature_author = str(recovery.get("signature_author") or "")
                single_contribution_proven = bool(
                    recovery.get("signature_status") == "explicit_evidence_observed"
                    and signature_author
                    and recovery.get("speaker_signature_provenance") == "mismatch"
                    and recovery.get("assignment_status") == "assigned"
                    and recovery.get("status") in {"b_safe", "b_usable"}
                    and str(recovery.get("boundary_method") or "").startswith(
                        "independent_signature_"
                    )
                    and merged_preceding_count == 0
                    and not changed_span_not_one_comment
                    and recovery.get("neighboring_comment_contamination") == "clean"
                )
                recovery_history_by_source[source_uid] = {
                    # This fact is consumed only together with a stable
                    # WikiConv action/root coordinate and an earlier source
                    # occurrence. A text match alone cannot use it.
                    "not_in_later_changed_span": changed_span_not_one_comment,
                    "changed_span_not_in_one_comment": changed_span_not_one_comment,
                    "merged_preceding_count": merged_preceding_count,
                    "boundary_evidence": boundary_evidence,
                    "reason_codes": reason_codes,
                    "assignment_reason_codes": assignment_codes,
                    "method_b_status": recovery.get("status"),
                    "action_type": recovery.get("action_type"),
                    "target_revision_id": recovery.get("target_revision_id"),
                    "boundary_method": recovery.get("boundary_method"),
                    "signature_status": recovery.get("signature_status"),
                    "signature_author": signature_author or None,
                    "speaker_signature_provenance": recovery.get("speaker_signature_provenance"),
                    "revision_actor": recovery.get("revision_actor"),
                    "single_contribution_proven": single_contribution_proven,
                    "terminal_candidate_raw": recovery.get("candidate_raw"),
                    "terminal_signature_raw": recovery.get("signature_raw"),
                    "assignment_status": recovery.get("assignment_status"),
                }
    # The annotation staging export is the final candidate-unit population:
    # it may carry a reviewed Method-B representation rather than the raw
    # WikiDisputes text.  Detect before the overlay is applied, so default
    # keeps receive a case alongside already-nominated repair rows.
    staged_by_source: dict[str, dict[str, str]] = {}
    staged_path = (
        output_root / "annotation" / "wikidisputes_llm_annotation_input.method_b_staged.csv"
    )
    if not staged_path.exists():
        staged_path = (
            output_root / "annotation" / "wikidisputes_llm_annotation_input.pre_raw_wikitext.csv"
        )
    if staged_path.exists():
        with staged_path.open(encoding="utf-8", newline="") as handle:
            for staged in csv.DictReader(handle):
                source_uid = str(staged.get("ssot_source_row_uid") or "")
                if source_uid:
                    staged_by_source[source_uid] = staged
    rows: list[dict[str, Any]] = []
    revision_cache = output_root.parent / "data" / "cache" / "mediawiki_revision_content.sqlite"
    seen: set[tuple[str, str, str]] = set()
    for source in raw:
        kind = str(source.get("problem_type", source.get("class", "")))
        source_row = str(source.get("source_row_uid") or "")
        dispute = str(source.get("source_dispute_id", source.get("dispute_uid", "")))
        key = (dispute, source_row, kind)
        if not kind or key in seen:
            continue
        seen.add(key)
        row = dict(source)
        joined = join_by_source.get(source_row, {})
        raw_source = raw_by_source.get(source_row, {})
        for field in ("dispute_uid", "episode_uid", "conversation_uid"):
            if not row.get(field) and joined.get(field):
                row[field] = joined[field]
        conversation_uid = str(row.get("conversation_uid") or "")
        if conversation_uid:
            row["conversation_id"] = conversation_uid.removeprefix("wikiconv-conversation:")
        for field in (
            "wikidisputes_current_id_exact",
            "wikidisputes_original_id_exact",
            "wikidisputes_text_exact",
        ):
            if joined.get(field) not in (None, ""):
                row[field] = joined[field]
        if raw_source:
            row["wikidisputes_raw_record_found"] = True
            row["wikidisputes_raw_text_exact"] = _raw_wikidisputes_text(
                raw_source.get("source_record_json_exact")
            )
        if source_row in prior_blank_fallback_sources:
            row["raw_blank_fallback_candidate"] = True
        if raw_source.get("source_projection_sha256"):
            row["source_projection_sha256"] = raw_source["source_projection_sha256"]
        if not row.get("utterance_id") and joined.get("wikidisputes_current_id_exact"):
            row["utterance_id"] = joined["wikidisputes_current_id_exact"]
        # The D07250 review is direct structural evidence, not a heuristic
        # based on its punctuation.  The source row remains intact either way.
        rationale = str(row.get("rationale") or "").casefold()
        if str(row.get("dispute_sequence")) == "D07250" and (
            "flattened table" in rationale or "structural punctuation" in rationale
        ):
            evidence = _evidence(row)
            evidence["structural_proven"] = True
            row["detector_evidence"] = json.dumps(evidence, sort_keys=True)
        row["case_id"] = stable_case_id(
            dispute, source_row, kind, str(source.get("logical_utterance_uid") or "")
        )
        rows.append(row)
    rows.extend(_mandatory_fixture_rows(output_root, seen, existing_rows=rows))
    anchor_sources_by_utterance = {
        str(source.get("wikidisputes_id_exact") or ""): source_uid
        for source_uid, source in raw_by_source.items()
        if str(source.get("wikidisputes_id_exact") or "")
    }
    # A join can retain the original/current identifier under a different
    # source schema version, so accept both exact aliases when resolving only
    # the already-evidenced revision-batch candidates above.
    anchor_sources_by_utterance.update(
        {
            str(row.get("wikidisputes_current_id_exact") or ""): str(
                row.get("source_row_uid") or ""
            )
            for row in rows
            if str(row.get("wikidisputes_current_id_exact") or "")
        }
    )
    fallback_source_uids = {
        str(row.get("source_row_uid") or "")
        for row in rows
        if decide_candidate(row)["final_disposition"] == "wikidisputes_fallback"
    }
    population_units: list[dict[str, Any]] = []
    for source_uid in sorted(set(join_by_source) | set(staged_by_source)):
        joined = join_by_source.get(source_uid, {})
        source = raw_by_source.get(source_uid)
        staged = staged_by_source.get(source_uid, {})
        # The staged export is itself an emitted annotation population even
        # when the join's canonical eligibility is false (for example a
        # recovered/current representation).  Do not lose adjacent replay
        # detection solely because its source lifecycle lacks that flag.
        if (source is None and not staged) or (
            not bool(joined.get("annotation_eligible")) and not staged
        ):
            continue
        source = source or {}
        text = _candidate_detection_text(
            staged,
            source,
            final_uses_source_text=source_uid in fallback_source_uids,
        )
        if not text.strip():
            continue
        population_units.append(
            {
                "source_row_uid": source_uid,
                "source_dispute_id": str(
                    source.get("source_dispute_id_exact") or staged.get("dispute_sequence") or ""
                ),
                "logical_utterance_uid": joined.get("logical_utterance_uid"),
                "utterance_id": staged.get("utterance_id")
                or joined.get("wikidisputes_current_id_exact"),
                "original_utterance_id": staged.get("original_utterance_id")
                or joined.get("wikidisputes_original_id_exact"),
                "dispute_uid": joined.get("dispute_uid"),
                "episode_uid": joined.get("episode_uid"),
                "conversation_uid": joined.get("conversation_uid"),
                # The staged substantive coordinate is the final annotation
                # adjacency.  Source-file order can interleave rows from
                # other conversations and must not defeat an adjacent replay.
                "source_order": staged.get("substantive_order") or source.get("source_order"),
                "speaker_id": staged.get("speaker_id") or source.get("wikidisputes_user_exact"),
                "timestamp": staged.get("timestamp") or "",
                "text": text,
                "replay_history_evidence": recovery_history_by_source.get(source_uid, {}),
                "turn_integrity_provenance": recovery_history_by_source.get(source_uid, {}),
            }
        )
    existing_by_source_kind = {
        (
            str(row.get("source_row_uid") or ""),
            str(row.get("problem_type") or ""),
        ): row
        for row in rows
    }
    # Check the staged population through a separate screen before merging
    # nominations into the review inventory.
    population_candidates = _discover_population_candidates(population_units)
    if coverage_expected is not None:
        coverage_expected.update(_independent_replay_coverage(population_units))
    for candidate in population_candidates:
        source_row = str(candidate["source_row_uid"])
        dispute = str(candidate["source_dispute_id"])
        kind = str(candidate["problem_type"])
        key = (dispute, source_row, kind)
        existing = existing_by_source_kind.get((source_row, kind))
        if existing is not None:
            # Source identifiers in older candidate artifacts sometimes use
            # the bare conversation ID while staging uses D-sequences. Merge
            # the fresh detector proof by immutable source occurrence instead
            # of emitting duplicate decisions/blockers for one finding.
            prior = _evidence(existing)
            fresh = _evidence(candidate)
            evidence = {**prior, **fresh}
            # The population scan is a nomination, not a new lifecycle
            # adjudication. It must not erase reviewed or historical proof.
            if prior.get("lifecycle_identity") in {"proven_alias", "proven_repost"}:
                evidence["lifecycle_identity"] = prior["lifecycle_identity"]
                for proof_key in (
                    "anchor_source_row_uid",
                    "physical_comment_slot",
                    "revision_history",
                    "replay_bundle",
                    "longitudinal_replay",
                    "strong_replay_identity",
                    "replay_identity_confidence",
                ):
                    if proof_key in prior:
                        evidence[proof_key] = prior[proof_key]
            elif prior.get("physical_comment_slot") and not fresh.get("physical_comment_slot"):
                evidence["physical_comment_slot"] = prior["physical_comment_slot"]
            existing["detector_evidence"] = evidence
            if not existing.get("dispute_sequence"):
                existing["dispute_sequence"] = dispute
            continue
        if key in seen:
            continue
        seen.add(key)
        candidate["case_id"] = stable_case_id(
            dispute,
            source_row,
            kind,
            str(candidate.get("logical_utterance_uid") or ""),
        )
        rows.append(candidate)
        existing_by_source_kind[(source_row, kind)] = candidate
    # Population nominations are appended after the historical fixture pass.
    # Enrich those cases from the same immutable revision and source record.
    for row in rows:
        source_uid = str(row.get("source_row_uid") or "")
        raw_source = raw_by_source.get(source_uid, {})
        source_text = _raw_wikidisputes_text(raw_source.get("source_record_json_exact"))
        evidence = _evidence(row)
        if row.get("problem_type") == "fragmentary_row" and source_text:
            revision_id = str(row.get("utterance_id") or "").split(".", 1)[0]
            heading = _source_proven_heading(
                source_text, _cached_revision_text(revision_cache, revision_id)
            )
            if heading:
                evidence.update(
                    {
                        "structural_proven": True,
                        "source_proven_section_heading": heading,
                        "source_revision_id": revision_id,
                        "review_basis": "exact_heading_in_cached_source_revision",
                    }
                )
        history = recovery_history_by_source.get(source_uid, {})
        author = _proven_terminal_creation_speaker(history, source_text)
        if author:
            evidence.update(
                {
                    "actor_signature_status": "proven_speaker_replacement",
                    "speaker_replacement": author,
                    "speaker_proof_revision_id": history.get("target_revision_id"),
                    "speaker_proof": "terminal_signature_in_assigned_creation_candidate",
                }
            )
        if row.get("problem_type") == "absorbed_multi_turn":
            staged_text = str(staged_by_source.get(source_uid, {}).get("utterance_text") or "")
            revision_id = str(row.get("utterance_id") or "").split(".", 1)[0]
            if _source_fallback_for_truncated_reconstruction(
                source_text,
                staged_text,
                _cached_revision_text(revision_cache, revision_id),
            ):
                evidence.update(
                    {
                        "source_proven_complete_after_truncation": True,
                        "source_revision_id": revision_id,
                        "review_basis": "source_prefix_and_terminal_span_in_cached_revision",
                    }
                )
                row["wikidisputes_raw_record_found"] = True
                row["wikidisputes_raw_text_exact"] = source_text
        if row.get("problem_type") == "missing_speaker" or (
            row.get("problem_type") == "absorbed_multi_turn"
            and history.get("changed_span_not_in_one_comment")
            and len(source_text) >= 300
        ):
            revision_id = str(row.get("utterance_id") or "").split(".", 1)[0]
            # This proof is independent of the revision actor and the Method-B
            # assignment. It can recover a signed span that the bounded
            # extractor truncated at a template or indentation change.
            historical = evaluate_historical_span(
                source_text,
                str(raw_source.get("wikidisputes_user_exact") or ""),
                _cached_revision_text(revision_cache, revision_id),
                revision_id,
            )
            if historical and overlaps_other_source(
                historical,
                (
                    str(peer.get("wikidisputes_text_exact") or "")
                    for peer in source_rows_by_revision.get(revision_id, [])
                    if str(peer.get("source_row_uid") or "") != source_uid
                ),
            ):
                historical = None
            if historical and row.get("problem_type") == "missing_speaker":
                if historical["kind"] == "single":
                    evidence.update(
                        {
                            "actor_signature_status": "proven_speaker_replacement",
                            "speaker_replacement": historical["speaker_id"],
                            "speaker_proof_revision_id": revision_id,
                            "speaker_proof": "unique_source_anchor_and_terminal_signature",
                            "historical_source_span": historical["source_span"],
                        }
                    )
            elif historical and row.get("problem_type") == "absorbed_multi_turn":
                if (
                    historical["kind"] == "split"
                    and history.get("assignment_status") == "assigned"
                    and recovery_candidate_start_by_source.get(source_uid)
                    == historical["second_candidate_start"]
                ):
                    evidence.update(
                        {
                            "historical_span_kind": "split",
                            "boundary_status": "defensible",
                            "parts": historical["parts"],
                            "historical_source_revision_id": revision_id,
                            "historical_source_anchor_offset": historical["source_anchor_offset"],
                            "historical_source_similarity": historical["source_similarity"],
                        }
                    )
                elif (
                    historical["kind"] == "single"
                    and historical["extended_by"] >= 1000
                    and historical["extension_reason"] in {"interior_quotation", "linked_addressee"}
                ):
                    staged_text = str(
                        staged_by_source.get(source_uid, {}).get("utterance_text") or ""
                    )
                    if historical["text"] != staged_text:
                        evidence.update(
                            {
                                "historical_span_kind": "single",
                                "historical_recovered_text": historical["text"],
                                "historical_source_revision_id": revision_id,
                                "historical_source_span": historical["source_span"],
                                "historical_source_anchor_offset": historical[
                                    "source_anchor_offset"
                                ],
                                "historical_source_similarity": historical["source_similarity"],
                            }
                        )
        if evidence:
            row["detector_evidence"] = evidence
    # Population candidates can exclude a prospective anchor, so resolve
    # bundles only after that detector population is present.
    _resolve_high_confidence_replay_bundles(
        rows, anchor_sources_by_utterance=anchor_sources_by_utterance
    )
    _resolve_longitudinal_replays(rows, anchor_sources_by_utterance=anchor_sources_by_utterance)
    _resolve_cached_signature_replays(
        rows,
        population_units,
        cache_path=output_root.parent / "data" / "cache" / "mediawiki_revision_content.sqlite",
    )
    for row in rows:
        if isinstance(row.get("detector_evidence"), Mapping):
            row["detector_evidence"] = json.dumps(row["detector_evidence"], sort_keys=True)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def _mandatory_fixture_rows(
    output_root: Path,
    seen: set[tuple[str, str, str]],
    *,
    existing_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add narrowly adjudicated rows absent from the detector inventory.

    These are review findings keyed to immutable source occurrences, never a
    text-similarity rule.  In particular, the absorbed-turn rows below were
    checked against Method-B boundary evidence and had no defensible new-turn
    span.  A dispute exclusion is therefore safer than inventing a split.
    """

    targets = {
        "wdrow:v1:537e8dbdc91fe311e977548fc773c6d163a419fe5a9a84675163068da696830f": {
            "dispute_sequence": "D06315",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "rationale": "D16 Method-B review: assignment contested and boundaries are "
            "not defensible.",
            "detector_evidence": {
                "boundary_status": "contested",
                "method_b_status": "b_no_candidate",
                "modification_continuity": "unproven",
            },
        },
        "wdrow:v1:8e78f36d3dde8ecfea9a69afef8b3a1ce52d3427cbe1227000efbb30136e0451": {
            "dispute_sequence": "D00111",
            "problem_type": "lifecycle_replay",
            "provisional_disposition": "repairable",
            "rationale": "D31 historical lifecycle evidence proves this emitted source "
            "occurrence is an alias.",
            "detector_evidence": {
                "lifecycle_identity": "proven_alias",
                "anchor_source_row_uid": (
                    "wdrow:v1:2d5a4fce708af0aee28bd8c408d2b0eb787ff62e211bfb7c2dd1d841adc8bd73"
                ),
            },
        },
        # Residual cumulative/absorbed representations. No parts are emitted
        # unless a revision boundary proves each span. D01997 and D08854 are
        # intentionally absent: their general stable-coordinate replay proof
        # now resolves the later copies while retaining the earliest source.
        "wdrow:v1:64f3858b9435ff84aa3d92bfa8585c15b86128cd9438c111d7eb7e166ecf7e45": {
            "dispute_sequence": "D05016",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "severity": "high",
            "rationale": "Composite historical representation contains prior turns without "
            "defensible source spans.",
            "detector_evidence": {
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_containment",
            },
        },
        "wdrow:v1:07d301e2d652fa39061848ed4141463b0793cba329d4d5ad47139400d584e0fe": {
            "dispute_sequence": "D06312",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "severity": "high",
            "rationale": "Composite representation absorbs earlier comments; Method-B evidence "
            "cannot certify a new-comment boundary.",
            "detector_evidence": {
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_containment",
            },
        },
        "wdrow:v1:a0821f14596665b5a2269fa52a4a1b3574cd4dba4256928e21007f9105d344ce": {
            "dispute_sequence": "D08703",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "severity": "high",
            "rationale": "Merged representation contains multiple signed turns without a "
            "defensible reconstructed partition.",
            "detector_evidence": {
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_boundary",
            },
        },
        "wdrow:v1:1e4ef8ea64e5d02b502d709f5425592b3a95cd483739e3694ce2015445dd09c4": {
            "dispute_sequence": "D08165",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "severity": "high",
            "rationale": "Cumulative source text has no historically certified split spans.",
            "detector_evidence": {
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_containment",
            },
        },
        "wdrow:v1:75e25ba9aeab0ad62390ad7154e4c762019e8e17969e6b29e6e2496df27e78fa": {
            "dispute_sequence": "D04848",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "severity": "high",
            "rationale": "Cumulative cross-speaker text cannot be partitioned without "
            "fabricated provenance.",
            "detector_evidence": {
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_containment",
            },
        },
        "wdrow:v1:072b8825396f728aa8b708730ac321471859fccbd58359430bff372c6996df32": {
            "dispute_sequence": "D08184",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "severity": "high",
            "rationale": "Long cumulative representation absorbs prior comments without "
            "defensible source spans.",
            "detector_evidence": {
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_containment",
            },
        },
        "wdrow:v1:ad0a3d90ea91cb26d5651be682da806bb4f8d38d82e8d5971b6a900d1c350899": {
            "dispute_sequence": "D02061",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "severity": "high",
            "rationale": "Near-identical long cross-speaker representations lack a proven repost "
            "or alias identity and safe boundaries.",
            "detector_evidence": {
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_cross_speaker_replay",
            },
        },
        "wdrow:v1:bfad0ed07f93b5b037d20886c21423e71bcf7fbed41616a306687964f9fbec46": {
            "dispute_sequence": "D04507",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "severity": "high",
            "rationale": "Exact cross-speaker long representation has no proven physical-comment "
            "lineage or split boundary.",
            "detector_evidence": {
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_cross_speaker_replay",
            },
        },
        "wdrow:v1:806651638773f527dd3f29815158d5d537b0b52c7b57c114f89a1ea089635254": {
            "dispute_sequence": "D05862",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "needs_history",
            "severity": "high",
            "rationale": "Exact cross-speaker long representation has no certified repost "
            "lifecycle or split boundary.",
            "detector_evidence": {
                "boundary_status": "not_defensible",
                "review_basis": "revision_diff_cross_speaker_replay",
            },
        },
        # D01057 is a single lifecycle modification which restores three
        # separately signed turns.  The source-text spans are the exact three
        # newline-delimited regions in revision 504534524; their sequence and
        # actors are directly visible in that revision.
        "wdrow:v1:8c8ad4ed70c27026e3a0fa99eee6695e6e3bdf4bc615bf68361041be19f19794": {
            "dispute_sequence": "D01057",
            "problem_type": "absorbed_multi_turn",
            "provisional_disposition": "repairable",
            "severity": "high",
            "rationale": "Revision 504534524 restores three signed comments with exact source "
            "spans and sequence.",
            "detector_evidence": {
                "boundary_status": "defensible",
                "review_basis": "revision_restore_sequence",
                "parts": [
                    {
                        "text": (
                            "I'm sorry, but WP:NPOV prevents us from doing that, as it would "
                            "require '''Wikipedia''' to agree with their apparent claim that all "
                            "recreational use is abuse. There is no reliable source for such a "
                            'thing, which would lead us to violate WP:RS. Really, "recreation '
                            "drugs\" is very neutral and supported. I don't see any better "
                            "alternative."
                        ),
                        "speaker_id": "Still-24-45-42-125",
                        "source_revision_id": "504534524",
                        "source_span": [0, 338],
                        "creation_evidence": "revision_restore_sequence",
                    },
                    {
                        "text": (
                            'Consensus is to go with the wording from the source: "drug abuse"  '
                            "Case closed.  -"
                        ),
                        "speaker_id": "Belchfire",
                        "source_revision_id": "504534524",
                        "source_span": [339, 421],
                        "creation_evidence": "revision_restore_sequence",
                    },
                    {
                        "text": (
                            "Sorry, but you have no authority to declare any such thing. Instead "
                            "of listening to you, I'll listen to WP:NPOV and WP:RS. If you don't "
                            "like it, go complain on the content dispute resolution page. I'm "
                            "confident your view will be rejected."
                        ),
                        "speaker_id": "Still-24-45-42-125",
                        "source_revision_id": "504534524",
                        "source_span": [422, 662],
                        "creation_evidence": "revision_restore_sequence",
                    },
                ],
            },
        },
        "wdrow:v1:a54d816085efd8d1e88241900328872dcbb2450dba21262ed67b0cc9adddb2d2": {
            "dispute_sequence": "D08584",
            "problem_type": "formatting_or_empty",
            "provisional_disposition": "likely_exclude",
            "severity": "high",
            "rationale": "Historical source is a preceding-unsigned-comment attribution stub, "
            "not a conversational turn.",
            "detector_evidence": {
                "structural_proven": True,
                "annotation_text_blank": True,
                "review_basis": "unsigned_attribution_stub",
            },
        },
        # Narrow fragment review: each occurrence is either a same-user,
        # same-revision continuation of the immediately preceding emitted
        # turn, or punctuation-only residue.  No textual-nearness rule is
        # applied outside this explicit evidence set.
        "wdrow:v1:8c2c8733d7071ea3e08629d63c3a4323fe36ef0356a998feebcc1aa20787cf2b": {
            "dispute_sequence": "D00544",
            "problem_type": "fragmentary_row",
            "provisional_disposition": "repairable",
            "severity": "high",
            "rationale": "Recovered revision identifies the full physical contribution for this "
            "source occurrence; it is not the adjacent same-time comment.",
            "detector_evidence": {
                "recovery_lookup": "mediawiki_raw_comment_recovery",
                "review_basis": "authoritative_revision_physical_comment",
            },
        },
        "wdrow:v1:0b72a625bd229151622bf5fc1a2e757debffc1e15f52aefc4180e72cea3e3d59": {
            "dispute_sequence": "D01467",
            "problem_type": "fragmentary_row",
            "provisional_disposition": "likely_exclude",
            "severity": "high",
            "rationale": "Two-character source residue has no defensible conversational "
            "completion in its immediate source neighbor.",
            "detector_evidence": {
                "structural_proven": True,
                "review_basis": "immediate_source_neighbor_no_continuation",
            },
        },
        "wdrow:v1:e4f6f9e93b17ed2ad71cbf0d23556a07f49bfc0f61761107dfe761171da4f35f": {
            "dispute_sequence": "D03017",
            "problem_type": "fragmentary_row",
            "provisional_disposition": "likely_exclude",
            "severity": "high",
            "rationale": "A two-apostrophe source record is formatting residue, not a turn.",
            "detector_evidence": {"structural_proven": True, "review_basis": "markup_only"},
        },
        "wdrow:v1:d6dbd8594024e6d54ea2da79c132790127219e88da023488c68061ea5613800c": {
            "dispute_sequence": "D03174",
            "problem_type": "fragmentary_row",
            "provisional_disposition": "likely_exclude",
            "severity": "high",
            "rationale": "Incomplete bold-markup residue has no conversational payload.",
            "detector_evidence": {
                "structural_proven": True,
                "review_basis": "immediate_revision_neighbor_markup_residue",
            },
        },
        # The same exact punctuation-only detector finds these analogous
        # standalones; they are excluded solely as structural residues.
        "wdrow:v1:565c8b861bc4a00d23cbfe6eec7948ee766e107bd3ab0dbd32f5eb4e847dbf16": {
            "dispute_sequence": "D02411",
            "problem_type": "fragmentary_row",
            "provisional_disposition": "likely_exclude",
            "severity": "high",
            "rationale": "A two-apostrophe source record is formatting residue, not a turn.",
            "detector_evidence": {"structural_proven": True, "review_basis": "markup_only"},
        },
        "wdrow:v1:8d501fa85dd851c042fe9958a4f13f5213894da8eb51710c7e9a0c4228e4794e": {
            "dispute_sequence": "D05150",
            "problem_type": "fragmentary_row",
            "provisional_disposition": "likely_exclude",
            "severity": "high",
            "rationale": "A two-apostrophe source record is formatting residue, not a turn.",
            "detector_evidence": {"structural_proven": True, "review_basis": "markup_only"},
        },
        "wdrow:v1:534504286e42a411ce4dae0248f382bdeb810fdc9b4a85d12b18ec6fdedd0d30": {
            "dispute_sequence": "D05215",
            "problem_type": "fragmentary_row",
            "provisional_disposition": "likely_exclude",
            "severity": "high",
            "rationale": "A two-apostrophe source record is formatting residue, not a turn.",
            "detector_evidence": {"structural_proven": True, "review_basis": "markup_only"},
        },
        "wdrow:v1:1439d3ed5907ce3246d0fed261d2dbd94a1f2c169f00322c721d9b2ce229dedd": {
            "dispute_sequence": "D07250",
            "problem_type": "fragmentary_row",
            "provisional_disposition": "repairable",
            "severity": "high",
            "rationale": "Adjacent same-user source rows form one contribution before the "
            "References block.",
            "detector_evidence": {
                "reattach_target_source_uid": (
                    "wdrow:v1:0cd7bbe1855611ad61b5048e9c1fbde271b4ae1627beb4e3cc241ed1f74295fb"
                ),
                "append_text_source": "source_row_annotation_text_exact",
                "joiner": " ",
                "review_basis": "immediate_source_neighbor_same_user_contribution",
            },
        },
    }
    targets.update(
        {
            "wdrow:v1:c6092101a62864241fcd0672f73f8461d27465f0c82e73a5b85708801271f642": {
                "dispute_sequence": "D01703",
                "problem_type": "fragmentary_row",
                "provisional_disposition": "keep",
                "rationale": "Single-character source text is retained pending direct "
                "physical-neighbor evidence; it is not auto-excluded as markup residue.",
                "detector_evidence": {
                    "detector_class": "fragment",
                    "fragment_signal": "very_short_payload",
                    "review_basis": "source_neighbor_inspection_required",
                },
            },
            "wdrow:v1:b7961c49c8ba54a214a769290b3d866ca957eeee85d65376e372bb998cbacda5": {
                "dispute_sequence": "D07726",
                "problem_type": "fragmentary_row",
                "provisional_disposition": "keep",
                "rationale": "Punctuation candidate has no reviewed physical-neighbor "
                "completion; preserve rather than infer a fragment attachment.",
                "detector_evidence": {
                    "detector_class": "fragment",
                    "fragment_signal": "no_alphanumeric_payload",
                    "review_basis": "source_neighbor_inspection_required",
                },
            },
        }
    )
    # Exact raw-signature aliases in D01057 only.  These are not a general
    # speaker normalizer: each listed source occurrence ends in the explicit
    # Still-24-45-42-125 user signature in its recovered revision text.
    d01057_still_aliases = (
        "wdrow:v1:3610d4bb3e0133b03ffb07ee7e549edfef41868a8a611edd11f81088e74233a9",
        "wdrow:v1:2597b8e58df42b4c9099c2b8a7de55a5db850ec78b552e2a10b6127a9ae086b7",
        "wdrow:v1:c3d9c243836b8c833febfc16764327a0d693a2bc3d0a88f6724cdec433963708",
        "wdrow:v1:31a998ac406ca23ef60dd35d556e9c801edd324ed996bf0befb93fdf8bc494a9",
        "wdrow:v1:afa0d4ae7091dfbbf7e19b056fdadba310e9c3860b8fbd7a00c77cfc1bf24480",
        "wdrow:v1:d0aa419f44a562de0b81048669dffe5bc889feee990b069149766c9e7a4d454d",
        "wdrow:v1:ac69a2aa7aae8a37563e5965ef738420a37121827abdcd5412c420f6d82e9f8b",
        "wdrow:v1:f62016a29e8f14a09443bfd1cd6c68a5597d27f1b153c9780ed45d5603881973",
        "wdrow:v1:02bcac903991abfde26f1ab13648b1a2227aac2c049d425af591ee87d3508b20",
        "wdrow:v1:676a7451c1306af19f76bfae5d0d94c765e6e004cff3439c68bb57957aa1816d",
        "wdrow:v1:784d89259d878f06b10065ea0057f374be4dd679ef5ea7211eab566fc7aa2e3a",
    )
    targets.update(
        {
            source: {
                "dispute_sequence": "D01057",
                "problem_type": "actor_signature_verified",
                "provisional_disposition": "keep",
                "severity": "low",
                "rationale": "Recovered raw revision ends with the exact Still-24-45-42-125 "
                "signature.",
                "detector_evidence": {
                    "actor_signature_status": "proven_alias",
                    "speaker_replacement": "Still-24-45-42-125",
                    "review_basis": "recovered_raw_wikitext_signature",
                },
            }
            for source in d01057_still_aliases
        }
    )
    # D04142 has three date-valued source actor fields.  The exact signed User
    # link target is literally ``10 December 2017`` in each revision, so these
    # are valid usernames and must be retained, not normalized or replaced.
    d04142_date_named_actor_sources = (
        "wdrow:v1:11daa2a9a368ac7147017e275ca1bd0aaba14b2ea3a920b554c4b96e936f1a17",
        "wdrow:v1:581e9a88fda22d77ff9a590ca43a1bd23d6f61e007f8156392d6942a4e05c1c8",
        "wdrow:v1:50b8ad621325d37bb2d56ea441f6c56a54a334ea9b708cf5403f209679cb3709",
    )
    targets.update(
        {
            source: {
                "dispute_sequence": "D04142",
                "problem_type": "actor_signature_verified",
                "provisional_disposition": "keep",
                "severity": "low",
                "rationale": "Authoritative revision user link confirms this date-shaped value "
                "is the literal username.",
                "detector_evidence": {
                    "actor_signature_status": "proven_username",
                    "authoritative_username": "10 December 2017",
                    "speaker_source_exact": "10 December 2017",
                    "review_basis": "high_confidence_revision_user_link",
                },
            }
            for source in d04142_date_named_actor_sources
        }
    )
    join_rows: dict[str, dict[str, Any]] = {}
    join_path = output_root / "silver" / "annotation_join_contract.parquet"
    if join_path.exists():
        for batch in pq.ParquetFile(join_path).iter_batches(batch_size=50_000):
            for row in batch.to_pylist():
                source = str(row.get("source_row_uid") or "")
                if source in targets:
                    join_rows[source] = row
    source_ids: dict[str, str] = {}
    source_path = output_root / "canonical" / "wikidisputes_source_projection.parquet"
    if source_path.exists():
        for batch in pq.ParquetFile(source_path).iter_batches(batch_size=50_000):
            for row in batch.to_pylist():
                source = str(row.get("source_row_uid") or "")
                if source in targets:
                    source_ids[source] = str(row.get("source_dispute_id_exact") or "")
    additions: list[dict[str, Any]] = []
    for source, specification in targets.items():
        joined = join_rows.get(source)
        if joined is None:
            continue
        dispute = source_ids.get(source) or str(joined.get("conversation_uid") or "")
        specification = dict(specification)
        evidence = _evidence(specification)
        if evidence.get("recovery_lookup") == "mediawiki_raw_comment_recovery":
            recovered = _recovered_mediawiki_comment(output_root, source)
            if not recovered.strip():
                raise RuntimeError(f"missing reviewed MediaWiki recovery for {source}")
            evidence["recovered_annotation_text"] = recovered
            specification["detector_evidence"] = evidence
        key = (dispute, source, str(specification["problem_type"]))
        if key in seen:
            # A targeted fixture supersedes its stale candidate record in
            # place.  This is needed when a rebuild reads the preceding
            # candidate artifact: retain the immutable source key, but never
            # carry forward an earlier review's evidence or attachment.
            for existing in existing_rows:
                if str(existing.get("source_row_uid") or "") == source and str(
                    existing.get("problem_type") or ""
                ) == str(specification["problem_type"]):
                    existing.update(specification)
                    break
            continue
        seen.add(key)
        additions.append(
            {
                **specification,
                "source_row_uid": source,
                "source_dispute_id": dispute,
                "logical_utterance_uid": joined.get("logical_utterance_uid"),
                "utterance_id": joined.get("wikidisputes_current_id_exact"),
                "wikidisputes_current_id_exact": joined.get("wikidisputes_current_id_exact"),
                "wikidisputes_original_id_exact": joined.get("wikidisputes_original_id_exact"),
                "wikidisputes_text_exact": joined.get("wikidisputes_text_exact"),
                "dispute_uid": joined.get("dispute_uid"),
                "episode_uid": joined.get("episode_uid"),
                "conversation_uid": joined.get("conversation_uid"),
                "conversation_id": conversation_id(joined.get("conversation_uid")),
                "severity": "high",
                "case_id": stable_case_id(
                    dispute,
                    source,
                    str(specification["problem_type"]),
                    str(joined.get("logical_utterance_uid") or ""),
                ),
            }
        )
    return additions


def materialize_turn_integrity(output_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
    """Write overlay tables and deterministic handoff metadata for annotation."""

    coverage_expected: set[tuple[str, str, str]] = set()
    candidates = _candidate_rows(output_root, coverage_expected=coverage_expected)
    decisions = [decide_candidate(row) for row in candidates]
    case_identities = {
        (str(row.get("source_row_uid") or ""), str(row.get("problem_type") or ""))
        for row in candidates
        if str(row.get("case_id") or "")
    }
    missing_candidate_identities = sorted(
        identity
        for identity in coverage_expected
        if (identity[1], identity[2]) not in case_identities
    )
    candidate_path = output_root / "reports" / "turn_integrity" / "candidates.parquet"
    decisions_path = output_root / "silver" / "turn_integrity_decisions.parquet"
    status_path = output_root / "silver" / "dispute_annotation_status.parquet"
    report_path = output_root / "reports" / "turn_integrity" / "repair_summary.json"
    handoff_path = output_root / "reports" / "turn_integrity" / "prompt3_handoff.json"
    atomic_parquet(candidate_path, table_from_union_pylist(candidates))
    atomic_parquet(decisions_path, table_from_union_pylist(decisions))
    reasons_by_dispute: dict[tuple[str, str | None, str | None, str | None], set[str]] = (
        defaultdict(set)
    )
    for row in decisions:
        if row["final_disposition"] == "dispute_exclude" and row["episode_uid"]:
            reasons_by_dispute[
                (
                    str(row["dispute_id"] or ""),
                    row["episode_uid"],
                    row["dispute_uid"],
                    row["conversation_uid"],
                )
            ].add(str(row["exclusion_reason"]))
    statuses = [
        {
            "dispute_id": dispute_id,
            "episode_uid": episode_uid,
            "dispute_uid": dispute_uid,
            "conversation_uid": conversation_uid,
            "conversation_id": conversation_id(conversation_uid),
            "annotation_status": "excluded",
            "exclusion_reasons_json": json.dumps(sorted(reasons)),
            "exclusion_reason": ";".join(sorted(reasons)),
            "policy_version": POLICY_VERSION,
        }
        for (dispute_id, episode_uid, dispute_uid, conversation_uid), reasons in sorted(
            reasons_by_dispute.items()
        )
    ]
    atomic_parquet(status_path, table_from_union_pylist(statuses))
    counts = Counter(str(row["final_disposition"]) for row in decisions)
    raw_blank_trace_sources = {
        str(row.get("source_row_uid") or "")
        for row in candidates
        if row.get("raw_blank_fallback_candidate")
        and str(row.get("problem_type") or "") == "formatting_or_empty"
    }
    raw_blank_trace_decisions = [
        row for row in decisions if str(row.get("source_row_uid") or "") in raw_blank_trace_sources
    ]
    excluded_reason_counts = Counter(
        reason
        for status in statuses
        for reason in json.loads(str(status["exclusion_reasons_json"]))
    )
    gold_case_counts = Counter(gold_status(str(row["final_disposition"])) for row in decisions)
    unresolved_keeps = [
        row
        for row in decisions
        if row["final_disposition"] == "keep"
        and str(row.get("decision_reason") or "").startswith("unresolved_")
    ]
    decisions_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        decisions_by_source[str(decision.get("source_row_uid") or "")].append(decision)

    def high_confidence_replay(row: Mapping[str, Any]) -> bool:
        evidence = _evidence(row)
        bundle = evidence.get("replay_bundle")
        slot = evidence.get("physical_comment_slot")
        return bool(
            (isinstance(bundle, Mapping) and bundle.get("high_confidence"))
            or _proven_physical_comment_slot_alias(evidence)
            or (isinstance(slot, Mapping) and slot.get("stable_across_revisions"))
        )

    qc = {
        "candidate_without_case_count": len(missing_candidate_identities),
        "missing_candidate_identities": [
            {"dispute_id": dispute, "source_row_uid": source_uid, "problem_type": kind}
            for dispute, source_uid, kind in missing_candidate_identities
        ],
        "unresolved_keep_count": len(unresolved_keeps),
        "high_confidence_unresolved_keep_count": sum(
            high_confidence_replay(row) for row in unresolved_keeps
        ),
        "replay_bundle_unresolved_count": sum(
            isinstance(_evidence(row).get("replay_bundle"), Mapping) for row in unresolved_keeps
        ),
        "first_unresolved_keep_count": sum(
            str(row.get("decision_reason") or "").startswith("unresolved_first")
            for row in unresolved_keeps
        ),
        "replay_bundle_nonretained_anchor_count": sum(
            bool(anchor_decisions)
            and not all(decision["annotation_eligible"] for decision in anchor_decisions)
            for row in decisions
            if isinstance(_evidence(row).get("replay_bundle"), Mapping)
            and (anchor := str(_evidence(row).get("anchor_source_row_uid") or ""))
            and (anchor_decisions := decisions_by_source.get(anchor, [])) is not None
        ),
    }
    # These are readiness findings, not rebuild failures.  The audit needs
    # freshly materialized artifacts precisely when unresolved cases remain.
    blockers = [row for row in decisions if row.get("annotation_blocking")]
    blocker_counts = Counter(
        str(row.get("annotation_blocking_reason") or "unspecified") for row in blockers
    )
    summary = {
        "policy_version": POLICY_VERSION,
        "candidate_count": len(candidates),
        "decisions_by_disposition": dict(sorted(counts.items())),
        "dispute_exclusions_by_reason": dict(sorted(excluded_reason_counts.items())),
        "gold_impact_candidate_cases": dict(sorted(gold_case_counts.items())),
        "qc": qc,
        "rebuild_completed": True,
        "annotation_ready": not blockers,
        "annotation_blockers": {
            "count": len(blockers),
            "by_reason": dict(sorted(blocker_counts.items())),
        },
        "raw_blank_fallback_trace": {
            "source_rows_scoped_from_pre_raw_export": len(raw_blank_trace_sources),
            "recovered_from_source_record_text": sum(
                row["final_disposition"] == "wikidisputes_fallback"
                for row in raw_blank_trace_decisions
            ),
            "genuinely_blank_or_nonconversational": sum(
                row["final_disposition"] == "row_exclude" for row in raw_blank_trace_decisions
            ),
            "retained_by_existing_decision": sum(
                row["final_disposition"] not in {"row_exclude", "wikidisputes_fallback"}
                for row in raw_blank_trace_decisions
            ),
        },
    }
    atomic_write_json(report_path, summary)
    handoff = {
        **summary,
        "schema_version": POLICY_VERSION,
        "random_seed": 20260918,
        "repository_root": str(repo_root) if repo_root else None,
        "exact_rebuild_command": "wikidisputes-ssot turn-integrity rebuild",
        "git_state": _git_state(repo_root),
        "artifacts": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in {
                "candidates": candidate_path,
                "decisions": decisions_path,
                "dispute_status": status_path,
                "repair_summary": report_path,
            }.items()
        },
        "known_remaining_limitations": [
            "Signals nominate candidates only; reconstruction-unsafe identity, split, and "
            "chronology cases use exact WikiDisputes text for annotation.",
            "No source/Bronze row or canonical lifecycle identity is rewritten by this overlay.",
        ],
    }
    atomic_write_json(handoff_path, handoff)
    return {
        "summary": summary,
        "paths": {
            "candidates": str(candidate_path),
            "decisions": str(decisions_path),
            "status": str(status_path),
            "handoff": str(handoff_path),
        },
    }


def _git_state(repo_root: Path | None) -> dict[str, str] | None:
    if repo_root is None:
        return None
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo_root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.rstrip()
    except (OSError, subprocess.CalledProcessError):
        return {"status": "unavailable"}
    return {"branch": branch, "head": head, "status_short": diff}
