from __future__ import annotations

import datetime as dt
import gc
import html
import json
import re
from bisect import bisect_right
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from .constants import (
    CHRONOLOGY_VERSION,
    IDENTITY_VERSION,
    JOIN_CONTRACT_VERSION,
    REPRESENTATION_VERSION,
    SCHEMA_VERSION,
)
from .cross_label import materialize_cross_label_reconciliation
from .hashing import canonical_json_hash, sha256_bytes
from .io import (
    atomic_link_or_copy,
    atomic_parquet,
    atomic_write_json,
    file_descriptor,
    table_from_union_pylist,
)
from .representations import extract_links, extract_signature_evidence

_SIGNED_TIMESTAMP_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)
_SIGNED_TIMESTAMP_TIME = r"(?P<hour>[01]?\d|2[0-3])\s*:\s*(?P<minute>[0-5]\d)"
_SIGNED_TIMESTAMP_DAY = r"(?P<day>3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?"
_SIGNED_TIMESTAMP_YEAR = r"(?P<year>(?:19|20)\d{2})"
_SIGNED_TIMESTAMP_MONTH_FIELD = rf"(?P<month>{_SIGNED_TIMESTAMP_MONTH})\.?"
_SIGNED_TIMESTAMP_UTC = (
    r"(?:\(\s*(?:UTC|Coordinated\s+Universal\s+Time)\s*\)|"
    r"\[\s*(?:UTC|Coordinated\s+Universal\s+Time)\s*\])"
)

# Keep the explicit timezone requirement while accepting the historical
# orderings already recognized by revision_diff.boundaries.  Each form is a
# separate expression so field names remain readable to the decoder below.
_SIGNED_UTC_TIMESTAMP_PATTERNS = (
    re.compile(
        rf"\b{_SIGNED_TIMESTAMP_TIME}\s*,?\s+{_SIGNED_TIMESTAMP_DAY}\s+"
        rf"{_SIGNED_TIMESTAMP_MONTH_FIELD}\s+{_SIGNED_TIMESTAMP_YEAR}\s*[,;]?\s*"
        rf"{_SIGNED_TIMESTAMP_UTC}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_SIGNED_TIMESTAMP_DAY}\s+{_SIGNED_TIMESTAMP_MONTH_FIELD}\s+"
        rf"{_SIGNED_TIMESTAMP_YEAR}\s*[,;]?\s*(?:\[\s*)?{_SIGNED_TIMESTAMP_TIME}"
        rf"(?:\s*\])?\s*[,;]?\s*{_SIGNED_TIMESTAMP_UTC}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_SIGNED_TIMESTAMP_MONTH_FIELD}\s+{_SIGNED_TIMESTAMP_DAY}\s*,?\s+"
        rf"{_SIGNED_TIMESTAMP_YEAR}\s*[,;]?\s*(?:\[\s*)?{_SIGNED_TIMESTAMP_TIME}"
        rf"(?:\s*\])?\s*[,;]?\s*{_SIGNED_TIMESTAMP_UTC}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_SIGNED_TIMESTAMP_YEAR}\s+{_SIGNED_TIMESTAMP_MONTH_FIELD}\s+"
        rf"{_SIGNED_TIMESTAMP_DAY}\s*[,;]?\s*(?:\[\s*)?{_SIGNED_TIMESTAMP_TIME}"
        rf"(?:\s*\])?\s*[,;]?\s*{_SIGNED_TIMESTAMP_UTC}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_SIGNED_TIMESTAMP_TIME}\s*,?\s+{_SIGNED_TIMESTAMP_YEAR}\s+"
        rf"{_SIGNED_TIMESTAMP_MONTH_FIELD}\s+{_SIGNED_TIMESTAMP_DAY}\s*[,;]?\s*"
        rf"{_SIGNED_TIMESTAMP_UTC}",
        re.IGNORECASE,
    ),
)
_SIGNED_TIMESTAMP_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_AUTOSIGNED_MARKER = re.compile(
    r"(?:autosigned|\{\{\s*unsigned(?:2)?\b|"
    r"preceding\s+\[\[wikipedia:signatures\|unsigned\]\]\s+comment\s+added\s+by)",
    re.IGNORECASE,
)


def _uid(namespace: str, *parts: Any) -> str:
    return f"{namespace}:v1:" + canonical_json_hash(list(parts))


def _iso_from_unix(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return dt.datetime.fromtimestamp(float(value), tz=dt.UTC).isoformat()


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(dt.UTC)
    except ValueError:
        return None


def _id_parts(value: Any) -> tuple[int, int, int]:
    maximum = 2**63 - 1
    if not isinstance(value, str):
        return maximum, maximum, maximum
    result: list[int] = []
    for part in value.split(".")[:3]:
        try:
            result.append(int(part))
        except ValueError:
            result.append(maximum)
    return tuple((result + [maximum] * 3)[:3])  # type: ignore[return-value]


def _creation_order_key(creation: dict[str, Any], logical_uid: str) -> tuple[Any, ...]:
    """Create a deterministic *display* key without claiming unknown chronology."""

    created_at = _parse_iso(creation.get("created_at"))

    if created_at is not None:
        return (
            0,
            created_at,
            *_id_parts(creation.get("creation_id")),
            creation["source_order"],
            logical_uid,
        )

    return (
        1,
        dt.datetime.max.replace(tzinfo=dt.UTC),
        *_id_parts(creation.get("creation_id")),
        creation["source_order"],
        logical_uid,
    )


def _reply_constrained_display_order(
    logical_uids: list[str],
    creation_by_logical: dict[str, dict[str, Any]],
    reply_target_by_source: dict[str, str | None],
    action_creation_upper_bound_by_uid: dict[str, str | None] | None = None,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Order one conversation using creation times and resolved reply edges.

    Validated timestamps retain their strict temporal order. An unresolved
    utterance is placed after its latest known reply ancestor and before its
    earliest known reply descendant. For unresolved modifications, a normalized
    action time is an additional latest-possible creation bound. Reply edges
    then provide the remaining topological constraints. Unconnected unresolved
    rows retain a deterministic fallback, preferring the row with fewer direct
    replies inside the feasible interval.

    This is display inference only: it does not create timestamps or chronology
    ranks. Cyclic or timestamp-incompatible reply constraints are skipped
    deterministically because no linear order can satisfy them.
    """

    uids = sorted(set(logical_uids))
    uid_set = set(uids)
    created_at_by_uid = {
        uid: _parse_iso(creation_by_logical[uid].get("created_at")) for uid in uids
    }
    supplied_action_bounds = action_creation_upper_bound_by_uid or {}
    action_upper_bound_by_uid = {
        uid: (
            _parse_iso(supplied_action_bounds.get(uid)) if created_at_by_uid[uid] is None else None
        )
        for uid in uids
    }
    known_time_groups: dict[dt.datetime, list[str]] = defaultdict(list)
    for uid, created_at in created_at_by_uid.items():
        if created_at is not None:
            known_time_groups[created_at].append(uid)
    known_times = sorted(known_time_groups)
    known_group_by_uid: dict[str, int] = {}
    for group_index, timestamp in enumerate(known_times):
        known_time_groups[timestamp].sort(
            key=lambda uid: _creation_order_key(creation_by_logical[uid], uid)
        )
        for uid in known_time_groups[timestamp]:
            known_group_by_uid[uid] = group_index

    parent_by_child = {
        child: str(parent)
        for child, parent in reply_target_by_source.items()
        if child in uid_set and parent in uid_set and child != parent
    }
    children_by_parent: dict[str, set[str]] = defaultdict(set)
    for child, parent in parent_by_child.items():
        children_by_parent[parent].add(child)

    lower_group_by_uid: dict[str, int | None] = {}
    upper_group_by_uid: dict[str, int | None] = {}
    for uid in uids:
        if uid in known_group_by_uid:
            lower_group_by_uid[uid] = None
            upper_group_by_uid[uid] = None
            continue

        ancestor_groups: list[int] = []
        seen_ancestors = {uid}
        parent = parent_by_child.get(uid)
        while parent is not None and parent not in seen_ancestors:
            seen_ancestors.add(parent)
            if parent in known_group_by_uid:
                ancestor_groups.append(known_group_by_uid[parent])
            parent = parent_by_child.get(parent)

        descendant_groups: list[int] = []
        pending = list(children_by_parent.get(uid, set()))
        seen_descendants = {uid}
        while pending:
            child = pending.pop()
            if child in seen_descendants:
                continue
            seen_descendants.add(child)
            if child in known_group_by_uid:
                descendant_groups.append(known_group_by_uid[child])
            pending.extend(children_by_parent.get(child, set()))

        lower_group_by_uid[uid] = max(ancestor_groups) if ancestor_groups else None
        upper_group_by_uid[uid] = min(descendant_groups) if descendant_groups else None

    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)

    def path_exists(start: str, target: str) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            pending.extend(outgoing.get(node, set()))
        return False

    def add_constraint(before: str, after: str) -> bool:
        if before == after or after in outgoing[before]:
            return before != after
        if path_exists(after, before):
            return False
        outgoing[before].add(after)
        incoming[after].add(before)
        return True

    # A later validated creation time cannot precede an earlier one. Within an
    # exact-time simultaneity group, resolved reply edges take precedence over
    # the stable ID/source fallback. Chaining the resulting sequence keeps this
    # linear in the number of known utterances rather than building all-pairs
    # timestamp constraints.
    known_sequence: list[str] = []
    for timestamp in known_times:
        group = known_time_groups[timestamp]
        group_set = set(group)
        group_incoming: dict[str, set[str]] = {
            uid: (
                {parent_by_child[uid]}
                if uid in parent_by_child and parent_by_child[uid] in group_set
                else set()
            )
            for uid in group
        }
        group_ready = sorted(
            (uid for uid in group if not group_incoming[uid]),
            key=lambda uid: _creation_order_key(creation_by_logical[uid], uid),
        )
        group_order: list[str] = []
        while group_ready:
            uid = group_ready.pop(0)
            group_order.append(uid)
            for child in sorted(children_by_parent.get(uid, set()) & group_set):
                group_incoming[child].discard(uid)
                if not group_incoming[child]:
                    group_ready.append(child)
                    group_ready.sort(
                        key=lambda candidate: _creation_order_key(
                            creation_by_logical[candidate], candidate
                        )
                    )
        group_order.extend(
            sorted(
                group_set - set(group_order),
                key=lambda uid: _creation_order_key(creation_by_logical[uid], uid),
            )
        )
        known_sequence.extend(group_order)
    for earlier_uid, later_uid in pairwise(known_sequence):
        add_constraint(earlier_uid, later_uid)

    skipped_reply_constraints: dict[str, list[str]] = defaultdict(list)
    for child, parent in sorted(parent_by_child.items(), key=lambda item: (item[1], item[0])):
        if not add_constraint(parent, child):
            skipped_reply_constraints[child].append(parent)

    active_action_upper_bound_by_uid: dict[str, dt.datetime | None] = {}
    action_bound_status_by_uid: dict[str, str] = {}
    for uid in uids:
        action_bound = action_upper_bound_by_uid[uid]
        if action_bound is None:
            active_action_upper_bound_by_uid[uid] = None
            action_bound_status_by_uid[uid] = "not_applicable"
            continue
        later_group_index = bisect_right(known_times, action_bound)
        if later_group_index == len(known_times):
            active_action_upper_bound_by_uid[uid] = action_bound
            action_bound_status_by_uid[uid] = "applied_no_later_known_anchor"
            continue
        first_later_uid = known_time_groups[known_times[later_group_index]][0]
        if add_constraint(uid, first_later_uid):
            active_action_upper_bound_by_uid[uid] = action_bound
            action_bound_status_by_uid[uid] = "applied"
        else:
            active_action_upper_bound_by_uid[uid] = None
            action_bound_status_by_uid[uid] = "conflicts_with_reply_or_creation_bounds"

    maximum_group = len(known_times)

    def priority(uid: str) -> tuple[Any, ...]:
        creation = creation_by_logical[uid]
        if uid in known_group_by_uid:
            slot = 2 * known_group_by_uid[uid]
            placement_phase = 1
            reply_count = 0
        else:
            upper_group = upper_group_by_uid[uid]
            lower_group = lower_group_by_uid[uid]
            action_bound = active_action_upper_bound_by_uid[uid]
            upper_slots: list[int] = []
            if upper_group is not None:
                upper_slots.append(2 * upper_group)
            if action_bound is not None:
                upper_slots.append(2 * bisect_right(known_times, action_bound))
            if upper_slots:
                slot = min(upper_slots)
                placement_phase = 0
            elif lower_group is not None:
                slot = 2 * lower_group + 1
                placement_phase = 0
            else:
                slot = 2 * maximum_group + 1
                placement_phase = 0
            reply_count = len(children_by_parent.get(uid, set()))
        return (
            slot,
            placement_phase,
            reply_count,
            *_id_parts(creation.get("creation_id")),
            creation["source_order"],
            uid,
        )

    remaining_incoming = {uid: set(incoming.get(uid, set())) for uid in uids}
    ready = sorted((uid for uid in uids if not remaining_incoming[uid]), key=priority)
    ordered: list[str] = []
    while ready:
        uid = ready.pop(0)
        ordered.append(uid)
        newly_ready: list[str] = []
        for child in sorted(outgoing.get(uid, set())):
            remaining_incoming[child].discard(uid)
            if not remaining_incoming[child]:
                newly_ready.append(child)
        if newly_ready:
            ready.extend(newly_ready)
            ready.sort(key=priority)

    # add_constraint prevents cycles, so this guards only against malformed
    # inputs or a future change that bypasses it.
    if len(ordered) != len(uids):
        ordered.extend(sorted(set(uids) - set(ordered), key=priority))

    evidence: dict[str, dict[str, Any]] = {}
    for uid in uids:
        created_at = created_at_by_uid[uid]
        lower_group = lower_group_by_uid[uid]
        upper_group = upper_group_by_uid[uid]
        reply_upper_bound = known_times[upper_group] if upper_group is not None else None
        action_upper_bound = action_upper_bound_by_uid[uid]
        active_action_upper_bound = active_action_upper_bound_by_uid[uid]
        effective_upper_candidates = [
            value for value in (reply_upper_bound, active_action_upper_bound) if value is not None
        ]
        evidence[uid] = {
            "display_order_method": "reply_and_action_constrained_creation_bounds_v2",
            "direct_reply_count": len(children_by_parent.get(uid, set())),
            "reply_parent_logical_uid": parent_by_child.get(uid),
            "reply_lower_bound_utc": (
                known_times[lower_group].isoformat() if lower_group is not None else None
            ),
            "reply_upper_bound_utc": (
                reply_upper_bound.isoformat() if reply_upper_bound is not None else None
            ),
            "action_creation_upper_bound_utc": (
                action_upper_bound.isoformat() if action_upper_bound is not None else None
            ),
            "action_creation_upper_bound_semantics": (
                "modification_event_latest_possible_creation"
                if action_upper_bound is not None
                else None
            ),
            "action_bound_status": action_bound_status_by_uid[uid],
            "effective_display_upper_bound_utc": (
                min(effective_upper_candidates).isoformat() if effective_upper_candidates else None
            ),
            "unknown_time_placement": (
                None
                if created_at is not None
                else "reply_and_or_action_bounded_display_inference"
                if lower_group is not None
                or upper_group is not None
                or active_action_upper_bound is not None
                else "display_only_fewer_replies_then_deterministic_fallback"
            ),
            "skipped_reply_constraints": skipped_reply_constraints.get(uid, []),
        }
    return ordered, evidence


# MEDIAWIKI_REVISION_TIMESTAMP_FIX_V1
_WIKIDISPUTES_EASTERN = ZoneInfo("America/New_York")
_WIKIDISPUTES_LONDON = ZoneInfo("Europe/London")


class IdentityConflictError(RuntimeError):
    """Raised when equally authoritative lifecycle roots cannot be reconciled."""


def _reconcile_source_identities(
    rows: list[dict[str, Any]],
    wikiconv_alias_to_roots: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Propagate authoritative roots through exact source action-ID aliases.

    Source creation rows and lifecycle ``original_id`` values are authoritative
    at the source tier. WikiConv ancestor/original lifecycle roots are stronger.
    Text and signature evidence deliberately never enter this resolver.
    """

    wc_roots_by_alias = wikiconv_alias_to_roots or {}
    parent: dict[str, str] = {}

    def find(alias: str) -> str:
        parent.setdefault(alias, alias)
        while parent[alias] != alias:
            parent[alias] = parent[parent[alias]]
            alias = parent[alias]
        return alias

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    fallback_alias_by_row: dict[str, str] = {}
    for row in rows:
        source_uid = str(row["source_row_uid"])
        current = row.get("wikidisputes_id_exact")
        original = row.get("wikidisputes_original_id_exact")
        action_type = str(row.get("wikidisputes_type_exact") or "")
        aliases = _source_identity_aliases(row)
        if not aliases:
            fallback = "fallback:" + source_uid
            find(fallback)
            fallback_alias_by_row[source_uid] = fallback
            continue
        for alias in aliases:
            find(alias)
        if (
            action_type in {"modification", "restoration", "deletion"}
            and isinstance(current, str)
            and current
            and isinstance(original, str)
            and original
        ):
            union(current, original)

    aliases_by_component: dict[str, set[str]] = defaultdict(set)
    for alias in list(parent):
        aliases_by_component[find(alias)].add(alias)
    rows_by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        source_uid = str(row["source_row_uid"])
        aliases = _source_identity_aliases(row)
        representative = aliases[0] if aliases else fallback_alias_by_row[source_uid]
        rows_by_component[find(representative)].append(row)

    selected_by_component: dict[str, tuple[str, str, list[str]]] = {}
    resolved_conflicts: list[dict[str, Any]] = []
    for component, aliases in sorted(aliases_by_component.items()):
        wc_roots = {root for alias in aliases for root in wc_roots_by_alias.get(alias, set())}
        source_roots: set[str] = set()
        for row in rows_by_component[component]:
            current = row.get("wikidisputes_id_exact")
            original = row.get("wikidisputes_original_id_exact")
            action_type = str(row.get("wikidisputes_type_exact") or "")
            if action_type == "original" and isinstance(current, str) and current:
                source_roots.add(f"wikiconv:{current}")
            elif (
                action_type in {"modification", "restoration", "deletion"}
                and isinstance(original, str)
                and original
            ):
                source_roots.add(f"wikiconv:{original}")

        if len(wc_roots) > 1:
            raise IdentityConflictError(
                f"conflicting WikiConv roots {sorted(wc_roots)} for exact aliases {sorted(aliases)}"
            )
        if len(wc_roots) == 1:
            selected = next(iter(wc_roots))
            conflicting_source = sorted(source_roots - {selected})
            if conflicting_source:
                resolved_conflicts.append(
                    {
                        "aliases": sorted(aliases),
                        "selected_root": selected,
                        "rejected_source_roots": conflicting_source,
                        "resolution_method": "wikiconv_lifecycle_over_source_original_id",
                    }
                )
            selected_by_component[component] = (
                selected,
                "wikiconv_authoritative_lifecycle",
                sorted(wc_roots | source_roots),
            )
        elif len(source_roots) == 1:
            selected_by_component[component] = (
                next(iter(source_roots)),
                "source_authoritative_creation_root",
                sorted(source_roots),
            )
        elif len(source_roots) > 1:
            raise IdentityConflictError(
                f"conflicting source roots {sorted(source_roots)} for exact aliases "
                f"{sorted(aliases)}"
            )
        else:
            stable_alias = min(aliases, key=lambda value: (*_id_parts(value), value))
            selected_by_component[component] = (
                "wdutt:fallback:v1:" + canonical_json_hash(["immutable-alias", stable_alias]),
                "unresolved_exact_alias_fallback",
                [],
            )

    row_to_logical_uid: dict[str, str] = {}
    method_by_row: dict[str, str] = {}
    candidate_roots_by_row: dict[str, list[str]] = {}
    for row in rows:
        source_uid = str(row["source_row_uid"])
        aliases = _source_identity_aliases(row)
        representative = aliases[0] if aliases else fallback_alias_by_row[source_uid]
        selected, method, candidates = selected_by_component[find(representative)]
        row_to_logical_uid[source_uid] = selected
        method_by_row[source_uid] = method
        candidate_roots_by_row[source_uid] = candidates

    return {
        "row_to_logical_uid": row_to_logical_uid,
        "method_by_row": method_by_row,
        "candidate_roots_by_row": candidate_roots_by_row,
        "resolved_conflicts": resolved_conflicts,
    }


def _canonical_timestamp(value: Any) -> str | None:
    parsed = _parse_iso(value)
    return parsed.isoformat() if parsed is not None else None


def _normalize_wikidisputes_creation_timestamp(
    value: Any,
    *,
    preferred_utc: Any = None,
) -> tuple[str | None, str]:
    """Interpret WikiDisputes timestamp text as Europe/London wall time."""

    if not isinstance(value, str) or not value:
        return None, "wikidisputes_creation_time_unavailable"
    try:
        wall = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None, "wikidisputes_creation_time_invalid"

    candidates: dict[str, dt.datetime] = {}
    for fold in (0, 1):
        localized = wall.replace(tzinfo=_WIKIDISPUTES_LONDON, fold=fold)
        candidate = localized.astimezone(dt.UTC)
        if candidate.astimezone(_WIKIDISPUTES_LONDON).replace(tzinfo=None) == wall:
            candidates[candidate.isoformat()] = candidate

    if len(candidates) == 1:
        return (
            next(iter(candidates.values())).isoformat(),
            "wikidisputes_creation_time_normalized_europe_london",
        )
    if len(candidates) > 1:
        preferred = _parse_iso(preferred_utc)
        if preferred is not None and preferred.isoformat() in candidates:
            return (
                preferred.isoformat(),
                "wikidisputes_creation_time_ambiguous_fold_resolved_by_stronger_evidence",
            )
        return None, "wikidisputes_creation_time_ambiguous_dst_fold"
    return None, "wikidisputes_creation_time_nonexistent_dst_gap"


def _repair_wikiconv_creation_timestamp(
    value: Any,
) -> tuple[str | None, str]:
    """Reverse the empirically validated WikiConv Eastern-time artifact.

    Calibration against MediaWiki revision timestamps showed that WikiConv
    creation timestamps are shifted later by exactly the DST-aware
    America/New_York UTC offset: +5h in EST and +4h in EDT.

    Around the spring DST transition the inverse may have two mathematically
    possible UTC values. Those cases are deliberately left unresolved rather
    than guessed.
    """
    shifted = _parse_iso(value)

    if shifted is None:
        return None, "wikiconv_creation_time_unavailable"

    candidates: list[dt.datetime] = []

    for hours in (4, 5):
        candidate = shifted - dt.timedelta(hours=hours)

        offset = candidate.astimezone(_WIKIDISPUTES_EASTERN).utcoffset()

        if offset is None:
            continue

        expected_hours = int(-offset.total_seconds() // 3600)

        if expected_hours == hours:
            candidates.append(candidate)

    unique = {candidate.isoformat(): candidate for candidate in candidates}

    if len(unique) == 1:
        repaired = next(iter(unique.values()))

        return (
            repaired.isoformat(),
            "wikiconv_creation_time_corrected_eastern_artifact",
        )

    if len(unique) > 1:
        return (
            None,
            "wikiconv_creation_time_dst_inverse_ambiguous",
        )

    return (
        None,
        "wikiconv_creation_time_timezone_repair_failed",
    )


def _resolve_creation_timestamp(
    *,
    creation_revision_id: int | None,
    creation_action: dict[str, Any] | None,
    original_source: dict[str, Any] | None,
    revision_timestamp_evidence: dict[int, str],
) -> tuple[str | None, str, str | None]:
    """Select validated creation time by precedence, never from a later action.

    The public tuple is retained for callers and historical tests.  Construction
    uses :func:`_resolve_creation_timestamp_evidence` below to retain the full
    fallthrough trail as provenance.
    """

    resolution = _resolve_creation_timestamp_evidence(
        creation_revision_id=creation_revision_id,
        creation_action=creation_action,
        original_source=original_source,
        revision_timestamp_evidence=revision_timestamp_evidence,
    )
    return (
        resolution["created_at_utc"],
        resolution["created_at_status"],
        resolution["raw_creation_evidence"],
    )


def _resolve_creation_timestamp_evidence(
    *,
    creation_revision_id: int | None,
    creation_action: dict[str, Any] | None,
    original_source: dict[str, Any] | None,
    revision_timestamp_evidence: dict[int, str],
    source_creation_authoritative: bool = True,
    signed_timestamp_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve creation time with validated, recorded fallthrough.

    A failed higher-priority tier is an observation, not a veto.  In
    particular, an invalid MediaWiki or WikiConv value must not suppress valid
    lower-tier creation evidence.  Only explicit creation lifecycle actions
    and a uniquely rooted authoritative WikiDisputes original can contribute
    raw creation values; modification/restoration/deletion timestamps cannot.
    """

    authoritative_creation_action = (
        creation_action if (creation_action or {}).get("action_type") == "creation" else None
    )
    authoritative_source_creation = (
        original_source
        if source_creation_authoritative
        and (original_source or {}).get("wikidisputes_type_exact") == "original"
        else None
    )
    attempts: list[dict[str, Any]] = []

    api_raw = (
        revision_timestamp_evidence.get(creation_revision_id)
        if creation_revision_id is not None
        else None
    )
    api_created_at = _canonical_timestamp(api_raw)
    if api_created_at is not None:
        return {
            "created_at_utc": api_created_at,
            "created_at_status": "mediawiki_revision_timestamp",
            "raw_creation_evidence": api_raw,
            "creation_time_source": "mediawiki_revision_timestamp",
            "creation_time_timezone": "UTC",
            "creation_time_semantics": "revision_creation",
            "creation_time_confidence": "high",
            "creation_historical_revision_id": creation_revision_id,
            "creation_evidence_attempts": attempts,
        }
    if creation_revision_id is not None:
        attempts.append(
            {
                "tier": "mediawiki_revision_timestamp",
                "status": "mediawiki_creation_timestamp_unavailable_or_invalid",
                "revision_id": creation_revision_id,
                "raw_timestamp": api_raw,
            }
        )

    wikiconv_raw = (
        _iso_from_unix(authoritative_creation_action.get("timestamp"))
        if authoritative_creation_action
        else None
    )
    if authoritative_creation_action:
        created_at, status = _repair_wikiconv_creation_timestamp(wikiconv_raw)
        if created_at is not None:
            return {
                "created_at_utc": created_at,
                "created_at_status": status,
                "raw_creation_evidence": wikiconv_raw,
                "creation_time_source": "wikiconv_creation_lifecycle",
                "creation_time_timezone": "America/New_York artifact corrected to UTC",
                "creation_time_semantics": "creation",
                "creation_time_confidence": "high",
                "creation_historical_revision_id": None,
                "creation_evidence_attempts": attempts,
            }
        attempts.append(
            {
                "tier": "wikiconv_creation_lifecycle",
                "status": status,
                "raw_timestamp": wikiconv_raw,
                "action_id": authoritative_creation_action.get("id"),
            }
        )

    source_raw = (
        authoritative_source_creation.get("wikidisputes_time")
        if authoritative_source_creation
        else None
    )
    if authoritative_source_creation:
        created_at, status = _normalize_wikidisputes_creation_timestamp(source_raw)
        if created_at is not None:
            return {
                "created_at_utc": created_at,
                "created_at_status": status,
                "raw_creation_evidence": source_raw,
                "creation_time_source": "wikidisputes_authoritative_root_creation",
                "creation_time_timezone": "Europe/London wall time normalized to UTC",
                "creation_time_semantics": "source_creation",
                "creation_time_confidence": "high",
                "creation_historical_revision_id": None,
                "creation_evidence_attempts": attempts,
            }
        attempts.append(
            {
                "tier": "wikidisputes_authoritative_root_creation",
                "status": status,
                "raw_timestamp": source_raw,
                "lifecycle_id": authoritative_source_creation.get("wikidisputes_id_exact"),
            }
        )
    elif original_source and not source_creation_authoritative:
        attempts.append(
            {
                "tier": "wikidisputes_authoritative_root_creation",
                "status": "wikidisputes_creation_root_ambiguous",
                "raw_timestamp": original_source.get("wikidisputes_time"),
            }
        )

    signed_candidate, signed_status = _select_structurally_localized_signed_timestamp(
        signed_timestamp_candidates or []
    )
    if signed_candidate is not None:
        return {
            "created_at_utc": signed_candidate["timestamp"],
            "created_at_status": signed_status,
            "raw_creation_evidence": signed_candidate["timestamp"],
            "creation_time_source": "historical_talk_page_signed_timestamp",
            "creation_time_timezone": "UTC",
            "creation_time_semantics": "signed_comment_creation",
            "creation_time_confidence": signed_candidate["confidence"],
            "creation_historical_revision_id": signed_candidate["historical_revision_id"],
            "creation_evidence_attempts": [
                *attempts,
                {
                    "tier": "historical_talk_page_signed_timestamp",
                    "status": signed_status,
                    **signed_candidate,
                },
            ],
        }
    if signed_timestamp_candidates:
        attempts.append(
            {
                "tier": "historical_talk_page_signed_timestamp",
                "status": signed_status,
                "candidate_count": len(signed_timestamp_candidates),
            }
        )

    return {
        "created_at_utc": None,
        "created_at_status": (
            str(attempts[-1]["status"])
            if any(attempt["tier"] != "mediawiki_revision_timestamp" for attempt in attempts)
            else "creation_timestamp_unresolved"
        ),
        "raw_creation_evidence": None,
        "creation_time_source": None,
        "creation_time_timezone": None,
        "creation_time_semantics": "creation",
        "creation_time_confidence": "none",
        "creation_historical_revision_id": None,
        "creation_evidence_attempts": attempts,
    }


def _normalize_lifecycle_event_time(
    value: Any,
    *,
    source: str,
) -> tuple[str | None, str, str]:
    """Normalize a lifecycle event time without repurposing its semantics."""

    if source == "wikiconv_nested_lifecycle":
        raw = _iso_from_unix(value)
        normalized, repair_status = _repair_wikiconv_creation_timestamp(raw)
        return (
            normalized,
            repair_status.replace("creation_time", "lifecycle_event_time"),
            "America/New_York artifact corrected to UTC",
        )
    if source == "wikidisputes_projection":
        normalized, status = _normalize_wikidisputes_creation_timestamp(value)
        return normalized, status.replace("creation_time", "lifecycle_event_time"), "Europe/London"
    raise ValueError(f"unknown lifecycle time source: {source}")


def _load_mediawiki_revision_timestamps(
    output_root: Path,
) -> dict[int, str]:
    """Load retained MediaWiki revision timestamps and fail on conflicts."""
    path = output_root.parent / "data" / "bronze" / "mediawiki_revision_timestamps.json"
    result: dict[int, str] = {}

    def add(revision_id: int, timestamp: str, source: str) -> None:
        previous = result.get(revision_id)
        if previous is not None and previous != timestamp:
            raise RuntimeError(
                "conflicting MediaWiki revision timestamps for "
                f"revision {revision_id}: {previous!r} versus {timestamp!r} "
                f"from {source}"
            )
        result[revision_id] = timestamp

    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))

        for key, value in payload.items():
            try:
                revision_id = int(key)
            except (TypeError, ValueError):
                continue

            if not isinstance(value, dict):
                continue

            timestamp = _canonical_timestamp(value.get("timestamp"))

            if value.get("status") == "found" and timestamp is not None:
                add(revision_id, timestamp, str(path))

    observations_path = output_root / "silver" / "talk_page_revision_observations.parquet"
    if not observations_path.exists():
        return result

    columns = ["revision_id", "timestamp", "availability_status"]
    observations = pq.ParquetFile(observations_path)
    for batch in observations.iter_batches(batch_size=10_000, columns=columns):
        values = batch.to_pydict()
        for revision_id, raw_timestamp, availability_status in zip(
            values["revision_id"],
            values["timestamp"],
            values["availability_status"],
            strict=True,
        ):
            if availability_status != "content_available":
                continue
            timestamp = _canonical_timestamp(raw_timestamp)
            if isinstance(revision_id, int) and timestamp is not None:
                add(revision_id, timestamp, str(observations_path))

    return result


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signature_timestamp_scan_text(value: str) -> str:
    """Remove formatting noise without treating arbitrary local time as UTC."""

    text = html.unescape(value).replace("\xa0", " ")
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    # Preserve an explicit timezone expressed as a wikilink before dropping
    # remaining formatting markup.  This is representation normalization, not
    # text/identity matching.
    text = re.sub(
        r"\[\[\s*(?:UTC|Coordinated\s+Universal\s+Time)"
        r"(?:\s*\|\s*(?:UTC|Coordinated\s+Universal\s+Time))?\s*\]\]",
        " (UTC) ",
        text,
        flags=re.IGNORECASE,
    )
    # Retain visible labels from harmless date-format links such as
    # ``[[11 April]] [[2016]]``.  The loader still requires one explicit UTC
    # timestamp in a Method-A-localized comment, so this cannot identify an
    # author or associate unrelated text.
    text = re.sub(
        r"\[\[\s*([^\]|]+?)\s*(?:\|\s*([^\]]+?)\s*)?\]\]",
        lambda match: match.group(2) or match.group(1),
        text,
    )
    text = re.sub(r"''+", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _parse_explicit_utc_signature_timestamps(value: Any) -> list[str]:
    """Return explicit-UTC signed timestamp candidates in textual order.

    This parser deliberately accepts only a date *and* time with an explicit
    UTC designator.  It permits ordinary historical date orderings, month
    abbreviations, punctuation and harmless wikitext/HTML formatting, but it
    never assigns a timezone to an otherwise-local or ambiguous timestamp.
    """

    if not isinstance(value, str):
        return []
    scan_text = _signature_timestamp_scan_text(value)
    matches: list[tuple[int, int, str]] = []
    for pattern in _SIGNED_UTC_TIMESTAMP_PATTERNS:
        for match in pattern.finditer(scan_text):
            month_key = str(match.group("month")).casefold().rstrip(".")[:3]
            month = _SIGNED_TIMESTAMP_MONTH_NUMBERS.get(month_key)
            if month is None:
                continue
            try:
                parsed = dt.datetime(
                    int(match.group("year")),
                    month,
                    int(match.group("day")),
                    int(match.group("hour")),
                    int(match.group("minute")),
                    tzinfo=dt.UTC,
                )
            except ValueError:
                continue
            matches.append((match.start(), match.end(), parsed.isoformat()))
    deduplicated = sorted(set(matches), key=lambda item: (item[0], item[1], item[2]))
    return [timestamp for _, _, timestamp in deduplicated]


def _parse_signed_utc_timestamp(value: Any) -> str | None:
    """Return one unambiguous explicit-UTC signature timestamp, if present."""

    candidates = _parse_explicit_utc_signature_timestamps(value)
    return candidates[0] if len(candidates) == 1 else None


def _load_structurally_localized_signed_timestamp_evidence(
    output_root: Path,
    _revision_timestamp_evidence: dict[int, str],
) -> dict[str, list[dict[str, Any]]]:
    """Load only defensibly localized, non-autosigned UTC signature evidence.

    The Method-A recovery artifact already binds a recovered raw comment to a
    source occurrence, a historical revision, text/boundary localization, and
    its quality measurements.  This loader adds no identity inference: it
    rejects uncertain segmentations, auto-signature boilerplate, multiple
    explicit-UTC timestamp markers, unavailable revision context, and
    timestamps later than the containing historical revision.  The recovery
    artifact's retained revision timestamp is itself the authoritative
    containing-revision context; a separate snapshot is supplemental and may
    legitimately omit old revisions.
    """

    path = output_root / "silver" / "mediawiki_raw_comment_recovery.parquet"
    if not path.exists():
        return {}

    columns = [
        "source_row_uid",
        "revision_id",
        "revision_timestamp",
        "recovery_status",
        "boundary_method",
        "target_coverage",
        "candidate_purity",
        "best_similarity",
        "second_similarity",
        "match_margin",
        "signature_residue_detected",
        "recovered_raw_wikitext",
        "recovery_tier",
        "candidate_provenance",
        "source_comparison_mode",
    ]
    evidence_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=10_000, columns=columns):
        for row in batch.to_pylist():
            source_row_uid = row.get("source_row_uid")
            raw_wikitext = row.get("recovered_raw_wikitext")
            boundary_method = row.get("boundary_method")
            if (
                not isinstance(source_row_uid, str)
                or not source_row_uid
                or row.get("recovery_status") != "high_confidence"
                or not isinstance(raw_wikitext, str)
                or not raw_wikitext
                or not isinstance(boundary_method, str)
                or _AUTOSIGNED_MARKER.search(raw_wikitext) is not None
            ):
                continue

            markers = _parse_explicit_utc_signature_timestamps(raw_wikitext)
            if len(markers) != 1:
                continue
            signed_at = markers[0]
            try:
                revision_id = int(row.get("revision_id"))
            except (TypeError, ValueError):
                continue
            declared_revision_time = _canonical_timestamp(row.get("revision_timestamp"))
            if declared_revision_time is None:
                continue
            signed_datetime = _parse_iso(signed_at)
            revision_datetime = _parse_iso(declared_revision_time)
            if (
                signed_datetime is None
                or revision_datetime is None
                or signed_datetime > revision_datetime
            ):
                continue

            evidence_by_source[source_row_uid].append(
                {
                    "timestamp": signed_at,
                    "association_status": "structurally_localized_signed_timestamp",
                    "confidence": "high",
                    "historical_revision_id": revision_id,
                    "historical_revision_timestamp": declared_revision_time,
                    "boundary_method": boundary_method,
                    "target_coverage": _as_float(row.get("target_coverage")),
                    "candidate_purity": _as_float(row.get("candidate_purity")),
                    "best_similarity": _as_float(row.get("best_similarity")),
                    "second_similarity": _as_float(row.get("second_similarity")),
                    "match_margin": _as_float(row.get("match_margin")),
                    "recovery_tier": row.get("recovery_tier"),
                    "candidate_provenance": row.get("candidate_provenance"),
                    "source_comparison_mode": row.get("source_comparison_mode"),
                }
            )
    return evidence_by_source


def _select_structurally_localized_signed_timestamp(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Accept exactly one signed creation time; retain ambiguity explicitly."""

    valid = [
        candidate
        for candidate in candidates
        if candidate.get("association_status") == "structurally_localized_signed_timestamp"
        and _canonical_timestamp(candidate.get("timestamp")) is not None
    ]
    if not valid:
        return None, "historical_signed_timestamp_not_available"
    by_timestamp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in valid:
        timestamp = _canonical_timestamp(candidate.get("timestamp"))
        if timestamp is not None:
            by_timestamp[timestamp].append(candidate)
    if len(by_timestamp) != 1:
        return None, "historical_signed_timestamp_ambiguous"
    timestamp, agreeing = next(iter(by_timestamp.items()))
    selected = min(
        agreeing,
        key=lambda candidate: (
            int(candidate.get("historical_revision_id") or 2**63 - 1),
            str(candidate.get("boundary_method") or ""),
        ),
    )
    return {**selected, "timestamp": timestamp, "corroborating_candidate_count": len(agreeing)}, (
        "historical_signed_timestamp_structurally_localized"
    )


def _write(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        table = table_from_union_pylist(rows)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        raise RuntimeError(f"cannot materialize {path.name}: {exc}") from exc
    atomic_parquet(path, table)
    del table
    pa.default_memory_pool().release_unused()
    return {**file_descriptor(path), "rows": len(rows)}


def _read_parquet_rows(
    path: Path, *, columns: list[str] | None = None, batch_size: int = 10_000
) -> list[dict[str, Any]]:
    """Read required Parquet rows without retaining a whole Arrow table too."""
    rows: list[dict[str, Any]] = []
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=batch_size, columns=columns):
        rows.extend(batch.to_pylist())
    return rows


def _append_only_registry(
    existing: list[dict[str, Any]], derived: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_uid = {str(row["registry_entry_uid"]): row for row in existing}
    for row in derived:
        by_uid.setdefault(str(row["registry_entry_uid"]), row)
    return list(by_uid.values())


def _source_logical_anchor(row: dict[str, Any]) -> str:
    """Stable source-occurrence anchor before WikiConv lifecycle resolution."""
    current = row.get("wikidisputes_id_exact")
    original = row.get("wikidisputes_original_id_exact")
    action_type = row.get("wikidisputes_type_exact")

    if (
        action_type in {"modification", "restoration", "deletion"}
        and isinstance(original, str)
        and original
    ):
        return original

    if isinstance(current, str) and current:
        return current

    if isinstance(original, str) and original:
        return original

    return "fallback:" + str(row["source_row_uid"])


def _source_identity_aliases(row: dict[str, Any]) -> list[str]:
    """Aliases usable for identity resolution without redirecting originals."""
    current = row.get("wikidisputes_id_exact")
    original = row.get("wikidisputes_original_id_exact")
    action_type = row.get("wikidisputes_type_exact")

    if action_type == "original":
        return [current] if isinstance(current, str) and current else []

    aliases: list[str] = []

    if isinstance(current, str) and current:
        aliases.append(current)

    if isinstance(original, str) and original:
        aliases.append(original)

    return aliases


def _is_context(row: dict[str, Any]) -> bool:
    """True only for a source conversation-header creation row."""
    return bool(
        row.get("wikidisputes_type_exact") == "original"
        and row.get("source_row_index") == 0
        and row.get("wikidisputes_id_exact")
        and row.get("wikidisputes_id_exact") == row.get("wikidisputes_conv_id_exact")
        and row.get("wikidisputes_reply_to_exact") is None
    )


def _wikiconv_lifecycle(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten WikiConv's nested action history without losing the parent row."""
    meta = json.loads(row["meta_json_canonical"])
    original = meta.get("original")
    actions: list[dict[str, Any]] = []
    if isinstance(original, dict):
        actions.append({"action_type": "creation", **original})
    else:
        current_id = row.get("wikiconv_id_exact")
        ancestor_id = row.get("ancestor_id_exact")
        later_actions_present = any(
            isinstance(value, list) and any(isinstance(item, dict) for item in value)
            for value in (
                meta.get("modification"),
                meta.get("deletion"),
                meta.get("restoration"),
            )
        )
        # A sparse nested record may still be a genuine root observation, but
        # current-row actor/time/text are creation evidence only when its exact
        # lifecycle identity proves it is the root.  In particular, do not let
        # a later current row with a distinct ancestor become the creator.
        root_current_observation = bool(
            current_id
            and current_id == ancestor_id
            and not row.get("parent_id_exact")
            and not later_actions_present
        )
        actions.append(
            {
                "action_type": "creation" if root_current_observation else "observed_current",
                "id": current_id,
                "speaker": row.get("wikiconv_speaker_exact"),
                "root": row.get("conversation_id_exact"),
                "reply_to": row.get("wikiconv_reply_to_exact"),
                "timestamp": row.get("wikiconv_timestamp_unix"),
                "text": row.get("wikiconv_text_exact"),
                "meta_dict": meta,
                "lifecycle_synthesis_status": (
                    "root_current_observation_exact_identity"
                    if root_current_observation
                    else "noncreation_current_observation_missing_nested_original"
                ),
            }
        )
    for field, action_type in (
        ("modification", "modification"),
        ("deletion", "deletion"),
        ("restoration", "restoration"),
    ):
        values = meta.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                actions.append({"action_type": action_type, **value})
    return actions


def _wikiconv_identity_aliases(row: dict[str, Any]) -> set[str]:
    """Return exact WikiConv aliases, including nested lifecycle action IDs."""

    aliases = {
        str(value)
        for value in (
            row.get("wikiconv_id_exact"),
            row.get("ancestor_id_exact"),
            row.get("parent_id_exact"),
        )
        if value
    }
    aliases.update(str(action["id"]) for action in _wikiconv_lifecycle(row) if action.get("id"))
    return aliases


def _resolve_reply_evidence(
    *,
    logical_uid: str,
    conversation_id: str,
    wikiconv_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    alias_to_logical: dict[tuple[str, str], set[str]],
) -> dict[str, Any]:
    """Resolve exact reply evidence without hiding cross-source disagreement.

    A uniquely resolved target may repair an unresolved exact target from the
    other source. Distinct uniquely resolved targets fail closed instead of
    selecting one silently. All observations remain serialized on the edge.
    """

    observations: list[dict[str, Any]] = []

    def observe(raw_target: Any, evidence_source: str, occurrence_uid: Any, rank: int) -> None:
        if not isinstance(raw_target, str) or not raw_target:
            return
        candidates = sorted(alias_to_logical.get((conversation_id, raw_target), set()))
        resolution_status = (
            "self_reference"
            if candidates == [logical_uid]
            else "resolved"
            if len(candidates) == 1
            else "ambiguous"
            if candidates
            else "unresolved"
        )
        observations.append(
            {
                "raw_target": raw_target,
                "evidence_source": evidence_source,
                "occurrence_uid": str(occurrence_uid) if occurrence_uid else None,
                "candidate_logical_uids": candidates,
                "resolution_status": resolution_status,
                "rank": rank,
            }
        )

    for row in wikiconv_rows:
        occurrence_uid = row.get("wikiconv_source_row_uid")
        for action in _wikiconv_lifecycle(row):
            if action.get("action_type") == "creation":
                observe(
                    action.get("reply_to"),
                    "wikiconv_creation_lifecycle",
                    occurrence_uid,
                    0,
                )
                break
        # The exact current-row target is independent reply evidence.  It may
        # be as authoritative as a nested creation target even when that row
        # cannot safely synthesize a creator/timestamp.
        observe(
            row.get("wikiconv_reply_to_exact"),
            "wikiconv_row",
            occurrence_uid,
            0,
        )

    for row in source_rows:
        observe(
            row.get("wikidisputes_reply_to_exact"),
            "wikidisputes_source_row",
            row.get("source_row_uid"),
            1,
        )

    observations.sort(
        key=lambda item: (
            int(item["rank"]),
            str(item["raw_target"]),
            str(item.get("occurrence_uid") or ""),
        )
    )
    resolved_by_rank: dict[int, set[str]] = defaultdict(set)
    for item in observations:
        if item["resolution_status"] == "resolved":
            resolved_by_rank[int(item["rank"])].add(str(item["candidate_logical_uids"][0]))
    resolved_targets = set().union(*resolved_by_rank.values()) if resolved_by_rank else set()

    target: str | None = None
    error_reason: str | None = None
    resolution_method = "none"
    selected_rank = min(resolved_by_rank) if resolved_by_rank else None
    preferred_targets = resolved_by_rank.get(selected_rank, set())
    if len(preferred_targets) == 1:
        target = next(iter(preferred_targets))
        matching = [item for item in observations if item["candidate_logical_uids"] == [target]]
        selected = matching[0]
        distinct_raw_targets = {str(item["raw_target"]) for item in observations}
        if len(resolved_targets) > 1:
            resolution_method = "preferred_creation_reply_evidence_with_disagreement"
        elif len(distinct_raw_targets) > 1:
            resolution_method = "unique_resolved_across_reply_evidence"
        else:
            resolution_method = "unique_conversation_scoped_alias"
    elif len(preferred_targets) > 1:
        selected = observations[0]
        error_reason = "conflicting_preferred_reply_targets"
    elif observations:
        selected = observations[0]
        error_reason = "no_unique_alias_or_context_target"
    else:
        selected = None

    return {
        "raw_target": selected["raw_target"] if selected else None,
        "target_logical_uid": target,
        "resolution_method": resolution_method,
        "resolution_status": (
            "resolved" if target else ("root_or_context" if not observations else "unresolved")
        ),
        "resolution_confidence": "high" if target else "none",
        "error_reason": error_reason,
        "reply_evidence_json": (
            json.dumps(
                {
                    "observations": [
                        {key: value for key, value in item.items() if key != "rank"}
                        for item in observations
                    ],
                    "preferred_evidence_rank": selected_rank,
                    "resolved_target_candidates": sorted(resolved_targets),
                },
                sort_keys=True,
            )
            if len({str(item["raw_target"]) for item in observations}) > 1
            or any(item["resolution_status"] != "resolved" for item in observations)
            or len(resolved_targets) > 1
            else None
        ),
    }


def _quarantine_forward_reply_target(
    resolution: dict[str, Any],
    *,
    child_time: dt.datetime | None,
    parent_time: dt.datetime | None,
) -> tuple[dict[str, Any], bool]:
    """Fail closed when a resolved reply target is chronologically impossible.

    The candidate target and prior resolution remain in the serialized evidence,
    but the edge is no longer presented as a resolved reply relationship. Neither
    creation timestamp is changed to make the relationship appear possible.
    """

    target = resolution.get("target_logical_uid")
    conflict = bool(target and child_time and parent_time and child_time < parent_time)
    if not conflict:
        return resolution, False

    evidence: dict[str, Any] = {}
    serialized_evidence = resolution.get("reply_evidence_json")
    if isinstance(serialized_evidence, str):
        try:
            parsed_evidence = json.loads(serialized_evidence)
        except json.JSONDecodeError:
            parsed_evidence = None
        if isinstance(parsed_evidence, dict):
            evidence = parsed_evidence
    evidence["chronology_conflict"] = {
        "candidate_target_logical_uid": target,
        "child_created_at_utc": child_time.isoformat() if child_time else None,
        "parent_created_at_utc": parent_time.isoformat() if parent_time else None,
        "prior_resolution_method": resolution.get("resolution_method"),
        "prior_resolution_status": resolution.get("resolution_status"),
    }

    quarantined = dict(resolution)
    quarantined.update(
        {
            "target_logical_uid": None,
            "resolution_method": "chronology_conflict_fail_closed",
            "resolution_status": "unresolved",
            "resolution_confidence": "none",
            "error_reason": "known_child_predates_known_parent",
            "reply_evidence_json": json.dumps(evidence, sort_keys=True),
        }
    )
    return quarantined, True


def _speaker_exact(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return str(value["id"])
    return None


def _logical_creator_speaker(rows: list[dict[str, Any]]) -> str | None:
    """Return the creator on authoritative lifecycle evidence, never the modifier."""

    for row in rows:
        creation = next(
            (
                action
                for action in _wikiconv_lifecycle(row)
                if action.get("action_type") == "creation"
            ),
            None,
        )
        speaker = _speaker_exact((creation or {}).get("speaker"))
        if speaker:
            return speaker
    return None


def _revision_id(value: Any, action_id: Any) -> int | None:
    candidate = value
    if candidate is None:
        candidate = _id_parts(action_id)[0]
    try:
        numeric = int(candidate)
    except (TypeError, ValueError):
        return None
    return numeric if numeric < 2**63 - 1 else None


def _episode_membership(
    output_root: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    episodes = _read_parquet_rows(output_root / "silver" / "dispute_episodes.parquet")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        grouped[str(episode["source_conversation_id_exact"])].append(episode)
    return grouped, episodes


def materialize_full_rehydrated(output_root: Path) -> dict[str, Any]:
    """Build the full selected-conversation logical/action bundle.

    This consumes only a *completed* annual WikiConv merge. It never promotes a
    partial scan to conversational completeness.
    """
    enumeration_report = json.loads(
        (output_root / "reports" / "conversation_enumeration.json").read_text(encoding="utf-8")
    )
    if enumeration_report["status"] not in {"complete", "gaps_or_conflicts"}:
        raise RuntimeError("WikiConv enumeration has no terminal coverage status")

    source = _read_parquet_rows(
        output_root / "canonical" / "wikidisputes_source_projection.parquet",
        columns=[
            "source_row_uid",
            "source_side",
            "source_wikidisputes_escalated",
            "source_row_index",
            "source_order",
            "source_record_json_exact",
            "wikidisputes_id_exact",
            "wikidisputes_original_id_exact",
            "wikidisputes_conv_id_exact",
            "wikidisputes_reply_to_exact",
            "wikidisputes_user_exact",
            "wikidisputes_time",
            "wikidisputes_type_exact",
            "wikidisputes_text_exact",
        ],
    )
    wikiconv_all = _read_parquet_rows(
        output_root / "silver" / "wikiconv_selected_rows.parquet",
        columns=[
            "corpus_year",
            "wikiconv_source_row_uid",
            "source_line_index",
            "source_record_sha256",
            "wikiconv_id_exact",
            "conversation_id_exact",
            "wikiconv_text_exact",
            "wikiconv_speaker_exact",
            "wikiconv_reply_to_exact",
            "wikiconv_timestamp_unix",
            "is_section_header",
            "indentation_exact",
            "ancestor_id_exact",
            "parent_id_exact",
            "meta_json_canonical",
        ],
    )

    # Preserve conflicting action observations; collapse only byte-identical
    # annual repeats of one WikiConv action identity.
    wc_by_observation: dict[tuple[str, str], dict[str, Any]] = {}
    for row in wikiconv_all:
        key = (str(row["wikiconv_id_exact"]), str(row["source_record_sha256"]))
        previous = wc_by_observation.get(key)
        if previous is None or int(row["corpus_year"]) < int(previous["corpus_year"]):
            wc_by_observation[key] = row
    wikiconv = list(wc_by_observation.values())

    # Retained, validated MediaWiki revision timestamp evidence.
    revision_timestamp_evidence = _load_mediawiki_revision_timestamps(output_root)
    signed_timestamp_evidence_by_source = _load_structurally_localized_signed_timestamp_evidence(
        output_root,
        revision_timestamp_evidence,
    )

    wc_context_by_uid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    wc_context_alias_to_uid: dict[str, set[str]] = defaultdict(set)
    wc_utterance_rows: list[dict[str, Any]] = []
    for row in wikiconv:
        if row.get("is_section_header") is True:
            anchor = str(row.get("ancestor_id_exact") or row["wikiconv_id_exact"])
            context_uid = f"wikiconv-context:{anchor}"
            wc_context_by_uid[context_uid].append(row)
            for value in (row.get("wikiconv_id_exact"), row.get("ancestor_id_exact")):
                if value:
                    wc_context_alias_to_uid[str(value)].add(context_uid)
            for lifecycle in _wikiconv_lifecycle(row):
                if lifecycle.get("id"):
                    wc_context_alias_to_uid[str(lifecycle["id"])].add(context_uid)
        else:
            wc_utterance_rows.append(row)
    source_to_context: dict[str, str] = {}
    context_source_uids: set[str] = set()

    # First identify context CREATIONS from WikiDisputes itself.
    # WikiConv section-header aliases may reconcile identity only after
    # source structure has established that the row is context.
    context_uid_by_creation_id: dict[str, str] = {}

    for row in source:
        if not _is_context(row):
            continue

        source_uid = str(row["source_row_uid"])
        current = str(row["wikidisputes_id_exact"])

        candidates: set[str] = set()
        for alias in _source_identity_aliases(row):
            candidates.update(wc_context_alias_to_uid.get(alias, set()))

        if len(candidates) == 1:
            context_uid = next(iter(candidates))
        else:
            context_uid = _uid(
                "wdcontext",
                "wikiconv-conversation:" + str(row.get("wikidisputes_conv_id_exact")),
                source_uid,
            )

        context_uid_by_creation_id[current] = context_uid
        context_source_uids.add(source_uid)
        source_to_context[source_uid] = context_uid

    # Modification/restoration/deletion rows are context only when their
    # original_id points to a source row already established as a context
    # creation.  They cannot become context merely from WikiConv alias overlap.
    for row in source:
        source_uid = str(row["source_row_uid"])

        if source_uid in context_source_uids:
            continue

        action_type = str(row.get("wikidisputes_type_exact") or "")
        original = row.get("wikidisputes_original_id_exact")

        if (
            action_type in {"modification", "restoration", "deletion"}
            and isinstance(original, str)
            and original in context_uid_by_creation_id
        ):
            context_uid = context_uid_by_creation_id[original]
            context_source_uids.add(source_uid)
            source_to_context[source_uid] = context_uid

    # WikiConv's section-header flag is not authoritative when exact source
    # lifecycle aliases establish that a row is a substantive comment. Promote
    # only those exact matches; no content similarity participates in identity.
    substantive_source_aliases = {
        alias
        for row in source
        if str(row["source_row_uid"]) not in context_source_uids
        for alias in _source_identity_aliases(row)
    }
    promoted_context_uids = {
        context_uid
        for context_uid, rows in wc_context_by_uid.items()
        if any(_wikiconv_identity_aliases(row) & substantive_source_aliases for row in rows)
    }
    for context_uid in promoted_context_uids:
        wc_utterance_rows.extend(wc_context_by_uid.pop(context_uid))
    if promoted_context_uids:
        wc_context_alias_to_uid = defaultdict(set)
        for context_uid, rows in wc_context_by_uid.items():
            for row in rows:
                for alias in _wikiconv_identity_aliases(row):
                    wc_context_alias_to_uid[alias].add(context_uid)

    wc_by_logical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    wc_alias_to_logical: dict[str, set[str]] = defaultdict(set)
    for row in wc_utterance_rows:
        anchor = str(row.get("ancestor_id_exact") or row["wikiconv_id_exact"])
        logical_uid = f"wikiconv:{anchor}"
        wc_by_logical[logical_uid].append(row)
        for alias in _wikiconv_identity_aliases(row):
            wc_alias_to_logical[alias].add(logical_uid)

    substantive_source_rows = [
        row for row in source if str(row["source_row_uid"]) not in context_source_uids
    ]
    source_identity = _reconcile_source_identities(
        substantive_source_rows,
        wc_alias_to_logical,
    )
    source_by_logical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in substantive_source_rows:
        resolved_logical = source_identity["row_to_logical_uid"][str(row["source_row_uid"])]
        source_by_logical[resolved_logical].append(row)

    all_logical_uids = sorted(set(wc_by_logical) | set(source_by_logical))
    episode_by_conversation, episode_rows = _episode_membership(output_root)
    outcome_rows = _read_parquet_rows(output_root / "silver" / "outcomes.parquet")
    outcomes_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcome_rows:
        outcomes_by_episode[str(outcome["episode_uid"])].append(outcome)

    utterances: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    context_actions: list[dict[str, Any]] = []
    context_representations: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    existing_registry_path = output_root / "silver" / "identity_registry.parquet"
    existing_registry = (
        _read_parquet_rows(existing_registry_path) if existing_registry_path.exists() else []
    )
    registry: list[dict[str, Any]] = []
    actor_rows: list[dict[str, Any]] = []
    signatures: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []
    for conflict in source_identity["resolved_conflicts"]:
        quality.append(
            {
                "quality_flag_uid": _uid(
                    "wdquality", "identity_root_conflict_resolved", conflict["aliases"]
                ),
                "entity_uid": conflict["selected_root"],
                "flag_code": "identity_root_conflict_resolved_by_stronger_lifecycle_evidence",
                "severity": "warning",
                "evidence_pointer": json.dumps(conflict, sort_keys=True),
            }
        )
    source_to_logical: dict[str, str] = {}
    source_action_resolution: dict[str, dict[str, Any]] = {}
    creation_by_logical: dict[str, dict[str, Any]] = {}
    identity_method_by_logical: dict[str, str] = {}

    for logical_uid in all_logical_uids:
        wc_rows = sorted(
            wc_by_logical.get(logical_uid, []),
            key=lambda row: (int(row["corpus_year"]), int(row["source_line_index"])),
        )
        source_rows = sorted(
            source_by_logical.get(logical_uid, []), key=lambda row: row["source_order"]
        )
        for row in source_rows:
            source_to_logical[str(row["source_row_uid"])] = logical_uid
        representative_wc = wc_rows[0] if wc_rows else None
        lifecycle_actions: list[dict[str, Any]] = []
        seen_lifecycle: set[tuple[str, str, str, str]] = set()
        for wc_row in wc_rows:
            for lifecycle in _wikiconv_lifecycle(wc_row):
                lifecycle_key = (
                    str(lifecycle.get("id")),
                    str(lifecycle.get("action_type")),
                    str(lifecycle.get("timestamp")),
                    canonical_json_hash(lifecycle),
                )
                if lifecycle_key in seen_lifecycle:
                    continue
                seen_lifecycle.add(lifecycle_key)
                lifecycle_actions.append(
                    {**lifecycle, "wikiconv_source_row_uid": wc_row["wikiconv_source_row_uid"]}
                )
        creation_action = next(
            (row for row in lifecycle_actions if row["action_type"] == "creation"), None
        )
        original_source_rows = [
            row for row in source_rows if row.get("wikidisputes_type_exact") == "original"
        ]
        source_creation_ids = {
            str(row["wikidisputes_id_exact"])
            for row in original_source_rows
            if row.get("wikidisputes_id_exact")
        }
        # A source timestamp is creation evidence only when the explicit
        # original lifecycle ID identifies one root.  This accepts repeated
        # observations of the same root but fails closed on distinct roots.
        source_creation_authoritative = bool(
            len(source_creation_ids) == 1
            and logical_uid == "wikiconv:" + next(iter(source_creation_ids))
        )
        original_source = (
            min(original_source_rows, key=lambda row: row["source_order"])
            if original_source_rows
            else None
        )
        conversation_id = str(
            (representative_wc or {}).get("conversation_id_exact")
            or (original_source or source_rows[0]).get("wikidisputes_conv_id_exact")
        )

        # Preserve all observed source ancestor aliases. A unique one can
        # recover creation provenance for source-only modification/restoration
        # rows without changing the logical-utterance identity.
        source_ancestor_ids = sorted(
            {
                str(row["wikidisputes_original_id_exact"])
                for row in source_rows
                if row.get("wikidisputes_original_id_exact")
            }
        )

        if creation_action:
            creation_id = str(creation_action.get("id"))

        elif original_source:
            # An original source row is itself the creation.
            creation_id = (
                str(original_source.get("wikidisputes_id_exact"))
                if original_source.get("wikidisputes_id_exact")
                else None
            )

        elif len(source_ancestor_ids) == 1:
            # Source-only modified/restored final observation.
            creation_id = source_ancestor_ids[0]

        else:
            creation_id = None

        creation_revision_id = _revision_id(
            None,
            creation_id,
        )

        creation_resolution = _resolve_creation_timestamp_evidence(
            creation_revision_id=creation_revision_id,
            creation_action=creation_action,
            original_source=original_source,
            revision_timestamp_evidence=revision_timestamp_evidence,
            source_creation_authoritative=source_creation_authoritative,
            signed_timestamp_candidates=[
                candidate
                for row in source_rows
                for candidate in signed_timestamp_evidence_by_source.get(
                    str(row["source_row_uid"]), []
                )
            ],
        )
        created_at = creation_resolution["created_at_utc"]
        created_at_status = creation_resolution["created_at_status"]
        raw_created_at = creation_resolution["raw_creation_evidence"]

        creation_by_logical[logical_uid] = {
            "conversation_id": conversation_id,
            "created_at": created_at,
            "created_at_status": created_at_status,
            "created_at_raw_evidence": raw_created_at,
            "creation_time_source": creation_resolution["creation_time_source"],
            "creation_time_timezone": creation_resolution["creation_time_timezone"],
            "creation_time_semantics": creation_resolution["creation_time_semantics"],
            "creation_time_confidence": creation_resolution["creation_time_confidence"],
            "creation_historical_revision_id": creation_resolution[
                "creation_historical_revision_id"
            ],
            "creation_evidence_attempts": creation_resolution["creation_evidence_attempts"],
            "creation_id": creation_id,
            "creation_revision_id": creation_revision_id,
            "source_order": min(
                (row["source_order"] for row in source_rows),
                default=2**63 - 1,
            ),
        }
        if created_at is None:
            quality.append(
                {
                    "quality_flag_uid": _uid(
                        "wdquality", logical_uid, "creation_timestamp_unresolved"
                    ),
                    "entity_uid": logical_uid,
                    "flag_code": created_at_status,
                    "severity": "warning",
                    "evidence_pointer": json.dumps(
                        {
                            "creation_id": creation_id,
                            "raw_creation_evidence": raw_created_at,
                        },
                        sort_keys=True,
                    ),
                }
            )

        source_methods = {
            source_identity["method_by_row"][str(row["source_row_uid"])] for row in source_rows
        }
        method = (
            "wikiconv_ancestor_id"
            if representative_wc
            else sorted(source_methods)[0]
            if source_methods
            else "wikiconv_only"
        )
        identity_method_by_logical[logical_uid] = method
        resolution_candidates = sorted(
            {
                candidate
                for row in source_rows
                for candidate in source_identity["candidate_roots_by_row"].get(
                    str(row["source_row_uid"]), []
                )
            }
        )
        registry.append(
            {
                "registry_entry_uid": _uid("wdregistry", logical_uid, IDENTITY_VERSION),
                "issued_uid": logical_uid,
                "entity_kind": "logical_utterance",
                "derivation_method": method,
                "selected_anchor": creation_id,
                "candidate_anchors_json": json.dumps(
                    resolution_candidates
                    or sorted(
                        {
                            str(value)
                            for row in wc_rows + source_rows
                            for value in (
                                row.get("wikiconv_id_exact"),
                                row.get("ancestor_id_exact"),
                                row.get("wikidisputes_id_exact"),
                                row.get("wikidisputes_original_id_exact"),
                            )
                            if value
                        }
                    ),
                    ensure_ascii=False,
                ),
                "confidence": "high"
                if representative_wc
                else "ambiguous"
                if resolution_candidates
                else "low",
                "adjudication_status": (
                    "not_required"
                    if representative_wc
                    else "pending_competing_candidates"
                    if resolution_candidates
                    else "pending"
                ),
                "algorithm_version": IDENTITY_VERSION,
                "effective_version": SCHEMA_VERSION,
                "registry_status": "active",
            }
        )

        wc_action_by_id: dict[str, dict[str, Any]] = {}
        if representative_wc:
            for lifecycle_index, lifecycle in enumerate(lifecycle_actions):
                action_id = str(lifecycle.get("id"))
                action_type = str(lifecycle["action_type"])
                action_uid = _uid("wdaction", logical_uid, action_type, action_id, lifecycle_index)
                version_uid = _uid("wdversion", action_uid)
                meta_dict = lifecycle.get("meta_dict")
                nested_meta = meta_dict if isinstance(meta_dict, dict) else {}
                raw_timestamp = _iso_from_unix(lifecycle.get("timestamp"))
                event_time_utc, event_time_status, event_time_timezone = (
                    _normalize_lifecycle_event_time(
                        lifecycle.get("timestamp"), source="wikiconv_nested_lifecycle"
                    )
                )
                action_row = {
                    "action_uid": action_uid,
                    "version_uid": version_uid,
                    "logical_utterance_uid": logical_uid,
                    "source_row_uid": None,
                    "source_row_uids_json": "[]",
                    "wikiconv_source_row_uid": lifecycle["wikiconv_source_row_uid"],
                    "action_type": action_type,
                    "action_id_exact": action_id,
                    "raw_timestamp": raw_timestamp,
                    "event_time_utc": event_time_utc,
                    "event_time_status": event_time_status,
                    "event_time_source": "wikiconv_nested_lifecycle",
                    "event_time_timezone": event_time_timezone,
                    "event_time_semantics": action_type,
                    "revision_id": _revision_id(nested_meta.get("rev_id"), lifecycle.get("id")),
                    "parent_action_id_exact": nested_meta.get("parent_id"),
                    "raw_action_json_canonical": json.dumps(
                        lifecycle, ensure_ascii=False, sort_keys=True, default=str
                    ),
                    "recovery_status": "recovered_from_pinned_wikiconv",
                    "recovery_method": "wikiconv_nested_lifecycle",
                    "schema_version": SCHEMA_VERSION,
                }
                actions.append(action_row)
                wc_action_by_id[action_id] = action_row
                action_text = lifecycle.get("text")
                action_encoded = (action_text or "").encode("utf-8")
                representations.append(
                    {
                        "representation_uid": _uid(
                            "wdrepr", version_uid, "wikiconv_action_text_exact"
                        ),
                        "logical_utterance_uid": logical_uid,
                        "version_uid": version_uid,
                        "source_row_uid": None,
                        "representation_kind": "wikiconv_action_text_exact",
                        "representation_scope": "logical_utterance_action_field",
                        "content_sha256": sha256_bytes(action_encoded),
                        "byte_length": len(action_encoded),
                        "encoding": "utf-8",
                        "mime_type": "text/plain",
                        "content_inline": action_text,
                        "blob_path": None,
                        "source_revision_id": (
                            str(action_row["revision_id"])
                            if action_row.get("revision_id") is not None
                            else None
                        ),
                        "extraction_method": "pinned_wikiconv_nested_action",
                        "extraction_version": "1.0.0",
                        "availability_status": "deleted_at_action"
                        if action_type == "deletion"
                        else "available",
                        "leakage_class": "action_time_state",
                        "available_at": action_row["event_time_utc"],
                        "confidence": "exact_wikiconv_action_field",
                        "representation_version": REPRESENTATION_VERSION,
                    }
                )
            text = representative_wc.get("wikiconv_text_exact")
            encoded = (text or "").encode("utf-8")
            current_action = wc_action_by_id.get(str(representative_wc["wikiconv_id_exact"]))
            if current_action is None:
                current_action = wc_action_by_id[str(creation_id)]
            version_uid = str(current_action["version_uid"])
            representation_uid = _uid("wdrepr", version_uid, "wikiconv_final_text")
            representations.append(
                {
                    "representation_uid": representation_uid,
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": None,
                    "representation_kind": "wikiconv_final_text_exact",
                    "representation_scope": "logical_utterance_final_field",
                    "content_sha256": sha256_bytes(encoded),
                    "byte_length": len(encoded),
                    "encoding": "utf-8",
                    "mime_type": "text/plain",
                    "content_inline": text,
                    "blob_path": None,
                    "source_revision_id": (
                        str(current_action["revision_id"])
                        if current_action.get("revision_id") is not None
                        else None
                    ),
                    "extraction_method": "pinned_wikiconv_json_decode",
                    "extraction_version": "1.0.0",
                    "availability_status": "available" if text is not None else "unknown",
                    "leakage_class": "final_state_not_predictor_safe",
                    "available_at": current_action.get("event_time_utc"),
                    "confidence": "exact_wikiconv_field",
                    "representation_version": REPRESENTATION_VERSION,
                }
            )
            signature = extract_signature_evidence(text or "")
            signature_uid = _uid("wdsignature", version_uid, signature["signature_status"])
            signatures.append(
                {
                    "signature_uid": signature_uid,
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    **signature,
                    "signature_html_reconstructed": None,
                    "parsed_signature_timestamp": None,
                    "actor_match_status": "not_testable_without_revision_actor",
                    "evidence_pointer": f"representation:{representation_uid}",
                    "confidence": "explicit_pattern"
                    if signature["raw_signature_wikitext"]
                    else "none",
                }
            )
            for link in extract_links(
                text or "", logical_utterance_uid=logical_uid, version_uid=version_uid
            ):
                links.append(
                    {
                        **link.__dict__,
                        "logical_utterance_uid": logical_uid,
                        "version_uid": version_uid,
                        "source_representation_uid": representation_uid,
                        "present_in_wikidisputes_text": None,
                        "recovered_from_revision": False,
                        "evidence_pointer": f"representation:{representation_uid}",
                        "confidence": "explicit_target_only",
                        "ambiguity": None,
                    }
                )
            actor_rows.append(
                {
                    "author_actor_uid": _uid("wdauthor", version_uid),
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": None,
                    "wikidisputes_user_exact": None,
                    "wikiconv_speaker_exact": _speaker_exact(
                        (creation_action or {}).get("speaker")
                    ),
                    "revision_actor_name_exact": None,
                    "revision_actor_user_id": None,
                    "identity_status": "wikiconv_speaker_only",
                    "resolved_identity": None,
                    "resolution_method": None,
                    "confidence": "unresolved",
                }
            )

        for row in source_rows:
            action_type = {
                "original": "creation",
                "modification": "modification",
                "restoration": "restoration",
                "deletion": "deletion",
            }.get(str(row.get("wikidisputes_type_exact")), "unknown")
            source_action_id = str(row.get("wikidisputes_id_exact"))
            matched_action = wc_action_by_id.get(source_action_id)
            if matched_action:
                source_uids = json.loads(matched_action["source_row_uids_json"])
                source_uids.append(row["source_row_uid"])
                matched_action["source_row_uids_json"] = json.dumps(sorted(set(source_uids)))
                source_action_resolution[str(row["source_row_uid"])] = matched_action
                version_uid = str(matched_action["version_uid"])
                source_event_time_utc = matched_action.get("event_time_utc")
            else:
                action_uid = _uid("wdaction", row["source_row_uid"])
                version_uid = _uid("wdversion", action_uid)
                raw_timestamp = row.get("wikidisputes_time")
                event_time_utc, event_time_status, event_time_timezone = (
                    _normalize_lifecycle_event_time(raw_timestamp, source="wikidisputes_projection")
                )
                source_action = {
                    "action_uid": action_uid,
                    "version_uid": version_uid,
                    "logical_utterance_uid": logical_uid,
                    "source_row_uid": row["source_row_uid"],
                    "source_row_uids_json": json.dumps([row["source_row_uid"]]),
                    "wikiconv_source_row_uid": None,
                    "action_type": action_type,
                    "action_id_exact": row.get("wikidisputes_id_exact"),
                    "raw_timestamp": raw_timestamp,
                    "event_time_utc": event_time_utc,
                    "event_time_status": event_time_status,
                    "event_time_source": "wikidisputes_projection",
                    "event_time_timezone": event_time_timezone,
                    "event_time_semantics": action_type,
                    "revision_id": _revision_id(None, row.get("wikidisputes_id_exact")),
                    "parent_action_id_exact": row.get("wikidisputes_original_id_exact"),
                    "raw_action_json_canonical": row["source_record_json_exact"],
                    "recovery_status": "source_projection_action",
                    "recovery_method": "wikidisputes_projection",
                    "schema_version": SCHEMA_VERSION,
                }
                actions.append(source_action)
                source_action_resolution[str(row["source_row_uid"])] = source_action
                source_event_time_utc = event_time_utc
            text = row.get("wikidisputes_text_exact")
            encoded = (text or "").encode("utf-8")
            source_representation_uid = _uid(
                "wdrepr", version_uid, "wikidisputes_text_exact", row["source_row_uid"]
            )
            representations.append(
                {
                    "representation_uid": source_representation_uid,
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": row["source_row_uid"],
                    "representation_kind": "wikidisputes_text_exact",
                    "representation_scope": "logical_utterance_source_field",
                    "content_sha256": sha256_bytes(encoded),
                    "byte_length": len(encoded),
                    "encoding": "utf-8",
                    "mime_type": "text/plain",
                    "content_inline": text,
                    "blob_path": None,
                    "source_revision_id": None,
                    "extraction_method": "json_decode_without_normalization",
                    "extraction_version": "1.0.0",
                    "availability_status": "available" if text is not None else "unknown",
                    "leakage_class": "source_available",
                    "available_at": source_event_time_utc,
                    "confidence": "exact_source_evidence",
                    "representation_version": REPRESENTATION_VERSION,
                }
            )
            source_signature = extract_signature_evidence(text or "")
            signatures.append(
                {
                    "signature_uid": _uid(
                        "wdsignature", version_uid, row["source_row_uid"], "source"
                    ),
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    **source_signature,
                    "signature_html_reconstructed": None,
                    "parsed_signature_timestamp": None,
                    "actor_match_status": "not_testable_from_source_projection",
                    "evidence_pointer": f"representation:{source_representation_uid}",
                    "confidence": "explicit_pattern"
                    if source_signature["raw_signature_wikitext"]
                    else "none",
                }
            )
            for source_link in extract_links(
                text or "", logical_utterance_uid=logical_uid, version_uid=version_uid
            ):
                links.append(
                    {
                        **source_link.__dict__,
                        "logical_utterance_uid": logical_uid,
                        "version_uid": version_uid,
                        "source_representation_uid": source_representation_uid,
                        "present_in_wikidisputes_text": True,
                        "recovered_from_revision": False,
                        "evidence_pointer": f"representation:{source_representation_uid}",
                        "confidence": "explicit_target_only",
                        "ambiguity": None,
                    }
                )
            actor_rows.append(
                {
                    "author_actor_uid": _uid("wdauthor", row["source_row_uid"]),
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_row_uid": row["source_row_uid"],
                    "wikidisputes_user_exact": row.get("wikidisputes_user_exact"),
                    "wikiconv_speaker_exact": None,
                    "revision_actor_name_exact": None,
                    "revision_actor_user_id": None,
                    "identity_status": "source_username_only",
                    "resolved_identity": None,
                    "resolution_method": None,
                    "confidence": "unresolved",
                }
            )

        for namespace, rows, columns in (
            (
                "wikiconv",
                wc_rows,
                ("wikiconv_id_exact", "ancestor_id_exact", "parent_id_exact"),
            ),
            (
                "wikidisputes",
                source_rows,
                (
                    "wikidisputes_id_exact",
                    "wikidisputes_original_id_exact",
                    "wikidisputes_reply_to_exact",
                ),
            ),
        ):
            for row in rows:
                occurrence = row.get("wikiconv_source_row_uid") or row.get("source_row_uid")
                for column in columns:
                    value = row.get(column)
                    if value is None:
                        continue
                    aliases.append(
                        {
                            "alias_uid": _uid("wdalias", occurrence, namespace, column, value),
                            "alias_namespace": f"{namespace}_{column}",
                            "alias_value_exact": value,
                            "source_row_uid": row.get("source_row_uid"),
                            "wikiconv_source_row_uid": row.get("wikiconv_source_row_uid"),
                            "entity_kind": "logical_utterance",
                            "resolved_entity_uid": logical_uid,
                            "resolution_status": "resolved"
                            if column not in {"wikidisputes_reply_to_exact", "parent_id_exact"}
                            else "target_alias_observed",
                            "validity_status": "observed",
                            "evidence_pointer": f"source_occurrence:{occurrence}",
                            "schema_version": SCHEMA_VERSION,
                        }
                    )
        for action_id, action_row in wc_action_by_id.items():
            aliases.append(
                {
                    "alias_uid": _uid(
                        "wdalias", action_row["action_uid"], "wikiconv_action_id", action_id
                    ),
                    "alias_namespace": "wikiconv_action_id",
                    "alias_value_exact": action_id,
                    "source_row_uid": None,
                    "wikiconv_source_row_uid": action_row["wikiconv_source_row_uid"],
                    "entity_kind": "utterance_action",
                    "resolved_entity_uid": logical_uid,
                    "resolved_action_uid": action_row["action_uid"],
                    "resolution_status": "resolved",
                    "validity_status": "observed",
                    "evidence_pointer": (
                        f"wikiconv_source_row:{action_row['wikiconv_source_row_uid']}"
                    ),
                    "schema_version": SCHEMA_VERSION,
                }
            )

    # Only validated creation times receive a chronology rank.  Unknown rows
    # retain a deterministic *display* order for human review, but that order
    # is never exported as a claim about temporal placement.
    chronology_rank_by_logical: dict[str, int | None] = {}
    display_utterance_order_by_logical: dict[str, int] = {}
    simultaneity_by_logical: dict[str, str | None] = {}
    grouped_logical: dict[str, list[str]] = defaultdict(list)
    for logical_uid, creation in creation_by_logical.items():
        grouped_logical[str(creation["conversation_id"])].append(logical_uid)
    for conversation_id, logical_uids in grouped_logical.items():
        eligible_uids = [
            uid for uid in logical_uids if _parse_iso(creation_by_logical[uid]["created_at"])
        ]
        eligible_uids.sort(key=lambda uid: _creation_order_key(creation_by_logical[uid], uid))
        eligible_uid_set = set(eligible_uids)
        unresolved_uids = [uid for uid in logical_uids if uid not in eligible_uid_set]
        unresolved_uids.sort(key=lambda uid: _creation_order_key(creation_by_logical[uid], uid))
        for rank, logical_uid in enumerate(eligible_uids, start=1):
            chronology_rank_by_logical[logical_uid] = rank
            display_utterance_order_by_logical[logical_uid] = rank
            timestamp = creation_by_logical[logical_uid]["created_at"]
            simultaneity_by_logical[logical_uid] = _uid(
                "wdsimultaneity",
                conversation_id,
                timestamp,
            )
        for display_order, logical_uid in enumerate(unresolved_uids, start=len(eligible_uids) + 1):
            chronology_rank_by_logical[logical_uid] = None
            display_utterance_order_by_logical[logical_uid] = display_order
            simultaneity_by_logical[logical_uid] = None

    metadata_by_conversation: dict[str, dict[str, Any]] = {}
    metadata_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    metadata_path = output_root / "silver" / "wikiconv_conversation_metadata.parquet"
    if metadata_path.exists():
        for observation in _read_parquet_rows(metadata_path):
            conversation_id = str(observation["conversation_id_exact"])
            metadata_by_conversation.setdefault(conversation_id, observation)
            metadata_candidates[conversation_id].append(observation)
    for conversation_id, candidates in metadata_candidates.items():
        hashes = sorted({str(row["metadata_sha256"]) for row in candidates})
        if len(hashes) > 1:
            quality.append(
                {
                    "quality_flag_uid": _uid(
                        "wdquality", conversation_id, "conversation_metadata_conflict"
                    ),
                    "entity_uid": "wikiconv-conversation:" + conversation_id,
                    "flag_code": "conversation_metadata_conflict",
                    "severity": "warning",
                    "evidence_pointer": json.dumps(hashes),
                }
            )
    disputes = _read_parquet_rows(output_root / "silver" / "disputes.parquet")
    dispute_by_uid = {str(row["dispute_uid"]): row for row in disputes}
    thread_uids_by_episode: dict[str, list[str]] = defaultdict(list)
    for thread in _read_parquet_rows(output_root / "silver" / "episode_threads.parquet"):
        thread_uids_by_episode[str(thread["episode_uid"])].append(str(thread["thread_uid"]))
    speakers_by_conversation: dict[str, set[str]] = defaultdict(set)
    for logical_uid, rows in wc_by_logical.items():
        conversation_id = str(creation_by_logical[logical_uid]["conversation_id"])
        for row in rows:
            speaker = _speaker_exact(row.get("wikiconv_speaker_exact"))
            if speaker:
                speakers_by_conversation[conversation_id].add(speaker.casefold())
    participant_split_keys_by_episode: dict[str, list[str]] = {}
    for episode in episode_rows:
        conversation_id = str(episode["source_conversation_id_exact"])
        observation = metadata_by_conversation.get(conversation_id)
        if observation:
            parsed = json.loads(str(observation["metadata_json_exact"]))
            metadata = parsed.get("meta", {}) if isinstance(parsed, dict) else {}
            episode["page_id_exact"] = metadata.get("page_id")
            wiki_title = metadata.get("page_title")
            source_title = episode.get("title_at_event_exact")
            title_matches = (
                isinstance(wiki_title, str)
                and isinstance(source_title, str)
                and wiki_title.replace("_", " ").casefold()
                == source_title.replace("_", " ").casefold()
            )
            episode["page_identity_match_status"] = (
                "page_id_observed_title_exact"
                if title_matches
                else "page_id_observed_title_differs"
            )
        else:
            episode["page_identity_match_status"] = "conversation_metadata_unavailable"
        logical_ids = grouped_logical.get(conversation_id, [])
        creation_times = [
            parsed
            for uid in logical_ids
            if (parsed := _parse_iso(creation_by_logical[uid]["created_at"])) is not None
        ]
        thread_start = min(creation_times) if creation_times else None
        episode["thread_start_at"] = thread_start.isoformat() if thread_start else None
        dispute = dispute_by_uid.get(str(episode["dispute_uid"]), {})
        dispute_metadata = json.loads(str(dispute.get("dispute_json_canonical", "{}")))
        source_participants: set[str] = set()
        users = dispute_metadata.get("users")
        if isinstance(users, list):
            source_participants.update(str(value).casefold() for value in users if value)
        for side in ("before", "after"):
            side_value = dispute_metadata.get(side)
            if isinstance(side_value, dict) and side_value.get("username"):
                source_participants.add(str(side_value["username"]).casefold())
        overlap = source_participants & speakers_by_conversation.get(conversation_id, set())
        observed_participant_aliases = source_participants | speakers_by_conversation.get(
            conversation_id, set()
        )
        participant_split_keys_by_episode[str(episode["episode_uid"])] = sorted(
            _uid("wdparticipant-alias-split", value) for value in observed_participant_aliases
        )
        episode["participant_overlap_count"] = len(overlap)
        episode["participant_overlap_status"] = (
            "observed_overlap"
            if overlap
            else "observed_none"
            if source_participants
            else "source_participants_not_observed"
        )
        event_time = _parse_iso(
            dispute_metadata.get("timestamp") or dispute_metadata.get("start_timestamp")
        )
        episode["temporal_distance_seconds"] = (
            (event_time - thread_start).total_seconds()
            if event_time is not None and thread_start is not None
            else None
        )
        episode["temporal_alignment_status"] = (
            "computed" if event_time is not None and thread_start is not None else "not_computed"
        )
        if (
            episode.get("page_identity_match_status") == "page_id_observed_title_exact"
            and logical_ids
        ):
            episode["alignment_status"] = "probable"
            episode["alignment_reasons"] = (
                "selected conversation observed; stable page ID and title match; "
                "section/link/move evidence not fully hydrated"
            )
        episode["analysis_rule_version"] = "probable-page-title-selected-conversation-v1"
        if (
            episode.get("alignment_status") in {"exact", "probable", "manually_verified"}
            and not str(episode.get("analysis_status", "")).startswith("quarantined")
            and episode.get("episode_index_at")
        ):
            episode["analysis_status"] = "eligible_probable_alignment_v1"
            episode["censoring_reason"] = None

    action_by_logical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        action_by_logical[str(action["logical_utterance_uid"])].append(action)
    for logical_uid, action_rows in action_by_logical.items():
        actions_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        children_by_parent: dict[str, list[str]] = defaultdict(list)
        for action in action_rows:
            action_id = str(action.get("action_id_exact"))
            actions_by_id[action_id].append(action)
            parent = action.get("parent_action_id_exact")
            if parent:
                children_by_parent[str(parent)].append(action_id)
        for action in action_rows:
            parent = action.get("parent_action_id_exact")
            if parent and str(parent) not in actions_by_id:
                quality.append(
                    {
                        "quality_flag_uid": _uid(
                            "wdquality", action["action_uid"], "missing_lifecycle_parent"
                        ),
                        "entity_uid": action["action_uid"],
                        "flag_code": "missing_lifecycle_parent",
                        "severity": "warning",
                        "evidence_pointer": f"parent_action_id:{parent}",
                    }
                )
        for parent, child_ids in children_by_parent.items():
            if len(set(child_ids)) > 1:
                quality.append(
                    {
                        "quality_flag_uid": _uid(
                            "wdquality", logical_uid, "concurrent_lifecycle_branch", parent
                        ),
                        "entity_uid": logical_uid,
                        "flag_code": "concurrent_lifecycle_branch",
                        "severity": "warning",
                        "evidence_pointer": json.dumps(sorted(set(child_ids))),
                    }
                )
        graph = {
            action_id: str(rows[0].get("parent_action_id_exact"))
            for action_id, rows in actions_by_id.items()
            if rows[0].get("parent_action_id_exact")
            and str(rows[0].get("parent_action_id_exact")) in actions_by_id
        }
        for start in graph:
            seen: set[str] = set()
            node: str | None = start
            while node in graph:
                if node in seen:
                    quality.append(
                        {
                            "quality_flag_uid": _uid(
                                "wdquality", logical_uid, "lifecycle_cycle", start
                            ),
                            "entity_uid": logical_uid,
                            "flag_code": "lifecycle_cycle",
                            "severity": "error",
                            "evidence_pointer": json.dumps(sorted(seen)),
                        }
                    )
                    break
                seen.add(node)
                node = graph.get(node)
        ordered_actions = sorted(
            action_rows,
            key=lambda row: (
                _parse_iso(row.get("event_time_utc")) or dt.datetime.max.replace(tzinfo=dt.UTC),
                str(row["action_uid"]),
            ),
        )
        deleted = False
        for action in ordered_actions:
            if action["action_type"] == "deletion":
                if deleted:
                    quality.append(
                        {
                            "quality_flag_uid": _uid(
                                "wdquality", action["action_uid"], "repeated_deletion"
                            ),
                            "entity_uid": action["action_uid"],
                            "flag_code": "repeated_deletion_without_restoration",
                            "severity": "warning",
                            "evidence_pointer": f"action:{action['action_uid']}",
                        }
                    )
                deleted = True
            elif action["action_type"] == "restoration":
                if not deleted:
                    quality.append(
                        {
                            "quality_flag_uid": _uid(
                                "wdquality", action["action_uid"], "restoration_without_deletion"
                            ),
                            "entity_uid": action["action_uid"],
                            "flag_code": "restoration_without_observed_deletion",
                            "severity": "warning",
                            "evidence_pointer": f"action:{action['action_uid']}",
                        }
                    )
                deleted = False
    representation_by_logical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for representation in representations:
        representation_by_logical[str(representation["logical_utterance_uid"])].append(
            representation
        )
    action_text_by_version: dict[str, dict[str, Any]] = {}
    representation_priority = {
        "wikidisputes_text_exact": 1,
        "wikiconv_action_text_exact": 2,
    }
    for row in representations:
        kind = str(row.get("representation_kind"))
        if kind not in representation_priority:
            continue
        version_uid = str(row["version_uid"])
        prior = action_text_by_version.get(version_uid)
        if (
            prior is None
            or representation_priority[kind]
            > representation_priority[str(prior["representation_kind"])]
        ):
            action_text_by_version[version_uid] = row

    def representation_at(
        logical_uid: str, cutoff: dt.datetime
    ) -> tuple[dict[str, Any] | None, bool]:
        candidates: list[tuple[dt.datetime, int, str, dict[str, Any]]] = []
        equal_time_uncertainty = False
        for action in action_by_logical[logical_uid]:
            action_time = _parse_iso(action.get("event_time_utc"))
            representation = action_text_by_version.get(str(action["version_uid"]))
            if action_time is None or representation is None:
                continue
            if action_time == cutoff and action.get("action_type") not in {
                "creation",
                "addition",
            }:
                equal_time_uncertainty = True
                continue
            if action_time > cutoff:
                continue
            revision = action.get("revision_id")
            candidates.append(
                (
                    action_time,
                    int(revision) if isinstance(revision, int) else -1,
                    str(action["action_uid"]),
                    representation,
                )
            )
        if not candidates:
            return None, equal_time_uncertainty
        return max(candidates, key=lambda item: item[:3])[3], equal_time_uncertainty

    for logical_uid in all_logical_uids:
        wc_rows = wc_by_logical.get(logical_uid, [])
        source_rows = source_by_logical.get(logical_uid, [])
        creation = creation_by_logical[logical_uid]
        action_rows = action_by_logical[logical_uid]
        types = [str(row["action_type"]) for row in action_rows]
        episodes = episode_by_conversation.get(str(creation["conversation_id"]), [])
        source_labels = sorted(
            {str(row["source_side"]) for row in source_rows if row.get("source_side")}
        )
        source_originals = [
            row for row in source_rows if row.get("wikidisputes_type_exact") == "original"
        ]
        final_repr = next(
            (
                row
                for row in representation_by_logical[logical_uid]
                if row["representation_kind"] == "wikiconv_final_text_exact"
            ),
            None,
        )
        creation_action_row = next(
            (row for row in action_rows if row["action_type"] == "creation"), None
        )
        creation_repr = next(
            (
                row
                for row in representation_by_logical[logical_uid]
                if creation_action_row
                and row["version_uid"] == creation_action_row["version_uid"]
                and row["representation_kind"]
                in {"wikiconv_action_text_exact", "wikidisputes_text_exact"}
            ),
            None,
        )
        source_repr = next(
            (
                row
                for row in representation_by_logical[logical_uid]
                if row["representation_kind"] == "wikidisputes_text_exact"
            ),
            None,
        )
        exact_source_row = (
            source_originals[0] if source_originals else source_rows[0] if source_rows else None
        )
        selected_representation = final_repr or source_repr or creation_repr
        utterances.append(
            {
                "logical_utterance_uid": logical_uid,
                "conversation_uid": "wikiconv-conversation:" + str(creation["conversation_id"]),
                "conversation_id_exact": creation["conversation_id"],
                "identity_method": identity_method_by_logical[logical_uid],
                "identity_algorithm_version": IDENTITY_VERSION,
                "created_at_utc": creation["created_at"],
                "created_at_status": creation["created_at_status"],
                "created_at_raw_evidence": creation.get("created_at_raw_evidence"),
                "creation_time_source": creation.get("creation_time_source"),
                "creation_time_timezone": creation.get("creation_time_timezone"),
                "creation_time_semantics": creation.get("creation_time_semantics"),
                "creation_time_confidence": creation.get("creation_time_confidence"),
                "creation_historical_revision_id": creation.get("creation_historical_revision_id"),
                "creation_evidence_attempts_json": json.dumps(
                    creation.get("creation_evidence_attempts", []), sort_keys=True
                ),
                "creation_revision_id": creation.get("creation_revision_id"),
                "chronology_eligible": creation["created_at"] is not None,
                "chronology_status": (
                    "eligible_validated_creation_time"
                    if creation["created_at"] is not None
                    else creation["created_at_status"]
                ),
                "chronology_rank": chronology_rank_by_logical[logical_uid],
                "ordering_evidence_json": json.dumps(
                    {
                        "known_creation_time": creation["created_at"] is not None,
                        "created_at_status": creation["created_at_status"],
                        "creation_id": creation.get("creation_id"),
                        "source_order": creation["source_order"],
                        "unknown_time_placement": (
                            None
                            if creation["created_at"] is not None
                            else "display_only_after_known_times_deterministic_fallback"
                        ),
                    },
                    sort_keys=True,
                ),
                # Legacy chronology field remains nullable rather than
                # presenting a deterministic fallback as actual chronology.
                "utterance_order": chronology_rank_by_logical[logical_uid],
                "display_utterance_order": display_utterance_order_by_logical[logical_uid],
                "simultaneity_group_id": simultaneity_by_logical[logical_uid],
                "in_wikidisputes_release": bool(source_rows),
                "in_source_projection_as_creation": bool(source_originals),
                "in_full_rehydrated_thread": bool(wc_rows),
                "additional_rehydrated_absent_from_wikidisputes": bool(wc_rows and not source_rows),
                "in_episode_window": False,
                "predictor_eligible": False,
                "outcome_eligible": False,
                "episode_membership_count": len(episodes),
                "primary_episode_uid": episodes[0]["episode_uid"] if len(episodes) == 1 else None,
                "source_label_provenance_json": json.dumps(source_labels),
                "source_row_count": len(source_rows),
                "wikidisputes_text_exact": (
                    exact_source_row.get("wikidisputes_text_exact") if exact_source_row else None
                ),
                "wikidisputes_user_exact": (
                    exact_source_row.get("wikidisputes_user_exact") if exact_source_row else None
                ),
                "wikidisputes_user_values_json": json.dumps(
                    sorted(
                        {
                            str(row["wikidisputes_user_exact"])
                            for row in source_rows
                            if row.get("wikidisputes_user_exact") is not None
                        }
                    ),
                    ensure_ascii=False,
                ),
                "wikiconv_speaker_exact": (_logical_creator_speaker(wc_rows)),
                "canonical_selected_text_sha256": (
                    selected_representation.get("content_sha256")
                    if selected_representation
                    else None
                ),
                "action_count": len(action_rows),
                "modification_count": types.count("modification"),
                "deletion_count": types.count("deletion"),
                "restoration_count": types.count("restoration"),
                "was_modified": "modification" in types,
                "modified_after_first_reply": None,
                "post_cutoff_modification": None,
                "wikidisputes_text_representation_uid": (
                    source_repr["representation_uid"] if source_repr else None
                ),
                "final_text_representation_uid": final_repr["representation_uid"]
                if final_repr
                else None,
                "creation_text_representation_uid": (
                    creation_repr["representation_uid"]
                    if creation_repr
                    else source_repr["representation_uid"]
                    if source_repr
                    else None
                ),
                "pre_first_reply_representation_uid": None,
                "pre_first_reply_equal_time_uncertainty": False,
                "predictor_cutoff_representation_uid": None,
                "predictor_selection_rule_version": ("historical-lifecycle-at-or-before-cutoff-v1"),
                "revision_wikitext_representation_uid": None,
                "rendered_html_reconstructed_representation_uid": None,
                "visible_text_reconstructed_representation_uid": None,
                "link_count": 0,
                "signature_count": 0,
                "link_child_key": f"logical_utterance_uid={logical_uid}",
                "signature_child_key": f"logical_utterance_uid={logical_uid}",
                "reply_target_logical_uid": None,
                "recovery_status": "recovered_from_wikiconv"
                if wc_rows
                else "source_only_unresolved",
                "recovery_method": "pinned_annual_corpus_union" if wc_rows else "source_projection",
                "available_at": creation["created_at"],
                "leakage_class": "creation_time_available",
                "quality_status": "unresolved" if not wc_rows else "recovered",
                "schema_version": SCHEMA_VERSION,
            }
        )

    selected_hash_to_logical: dict[str, list[str]] = defaultdict(list)
    for utterance in utterances:
        logical_uid = str(utterance["logical_utterance_uid"])
        selected_hash = utterance.get("canonical_selected_text_sha256")
        text = utterance.get("wikidisputes_text_exact")
        if not isinstance(text, str):
            final_representation = next(
                (
                    row
                    for row in representation_by_logical[logical_uid]
                    if row.get("representation_uid")
                    == utterance.get("final_text_representation_uid")
                ),
                None,
            )
            text = final_representation.get("content_inline") if final_representation else None
        if selected_hash and isinstance(text, str) and text:
            selected_hash_to_logical[str(selected_hash)].append(logical_uid)
        flag_specs: list[tuple[bool, str, str, str]] = [
            (
                not utterance.get("wikidisputes_user_exact")
                and not utterance.get("wikiconv_speaker_exact"),
                "missing_author_evidence",
                "warning",
                "neither source username nor WikiConv speaker is observed",
            ),
            (
                not isinstance(text, str) or not text,
                "empty_or_unavailable_text",
                "warning",
                "selected exact text is empty or unavailable",
            ),
            (
                isinstance(text, str) and len(text.split()) > 1000,
                "comment_over_1000_whitespace_tokens",
                "info",
                f"whitespace_tokens={len(text.split()) if isinstance(text, str) else 0}",
            ),
            (
                isinstance(text, str) and text.count("(UTC)") >= 2,
                "absorbed_multi_turn_candidate",
                "warning",
                "multiple signature timestamp markers; no automatic split",
            ),
        ]
        for applies, code, severity, evidence_pointer in flag_specs:
            if applies:
                quality.append(
                    {
                        "quality_flag_uid": _uid("wdquality", logical_uid, code),
                        "entity_uid": logical_uid,
                        "flag_code": code,
                        "severity": severity,
                        "evidence_pointer": evidence_pointer,
                    }
                )
    for content_hash, logical_uids in selected_hash_to_logical.items():
        if len(logical_uids) < 2:
            continue
        for logical_uid in logical_uids:
            quality.append(
                {
                    "quality_flag_uid": _uid(
                        "wdquality", logical_uid, "exact_text_duplicate_candidate", content_hash
                    ),
                    "entity_uid": logical_uid,
                    "flag_code": "exact_text_duplicate_candidate",
                    "severity": "info",
                    "evidence_pointer": json.dumps(
                        {"count": len(logical_uids), "sample_uids": sorted(logical_uids)[:20]}
                    ),
                }
            )
    # Reply aliases are conversation-scoped. WikiConv identifiers are usually
    # globally unique, but scoping prevents a repeated source alias in a
    # contradictory/cross-label record from resolving to the wrong thread.
    alias_to_logical: dict[tuple[str, str], set[str]] = defaultdict(set)
    for logical_uid in all_logical_uids:
        conversation_id = str(creation_by_logical[logical_uid]["conversation_id"])
        for row in wc_by_logical.get(logical_uid, []):
            for value in (
                row.get("wikiconv_id_exact"),
                row.get("ancestor_id_exact"),
                row.get("parent_id_exact"),
            ):
                if value:
                    alias_to_logical[(conversation_id, str(value))].add(logical_uid)
            for lifecycle in _wikiconv_lifecycle(row):
                if lifecycle.get("id"):
                    alias_to_logical[(conversation_id, str(lifecycle["id"]))].add(logical_uid)
        for row in source_by_logical.get(logical_uid, []):
            for value in (
                row.get("wikidisputes_id_exact"),
                row.get("wikidisputes_original_id_exact"),
            ):
                if value:
                    alias_to_logical[(conversation_id, str(value))].add(logical_uid)
    replies: list[dict[str, Any]] = []
    for logical_uid in all_logical_uids:
        wc = wc_by_logical.get(logical_uid, [])
        src = source_by_logical.get(logical_uid, [])
        representative = wc[0] if wc else (src[0] if src else {})
        conversation_id = str(creation_by_logical[logical_uid]["conversation_id"])
        reply_resolution = _resolve_reply_evidence(
            logical_uid=logical_uid,
            conversation_id=conversation_id,
            wikiconv_rows=wc,
            source_rows=src,
            alias_to_logical=alias_to_logical,
        )
        raw_target = reply_resolution["raw_target"]
        target = reply_resolution["target_logical_uid"]
        source_time = _parse_iso(creation_by_logical[logical_uid]["created_at"])
        target_time = _parse_iso(creation_by_logical[target]["created_at"]) if target else None
        reply_resolution, child_before_parent = _quarantine_forward_reply_target(
            reply_resolution,
            child_time=source_time,
            parent_time=target_time,
        )
        target = reply_resolution["target_logical_uid"]
        self_reference = target == logical_uid
        replies.append(
            {
                "reply_edge_uid": _uid("wdreply", logical_uid, raw_target),
                "source_logical_utterance_uid": logical_uid,
                "source_row_uid": src[0]["source_row_uid"] if src else None,
                "raw_reply_target": raw_target,
                "repaired_reply_target": raw_target if target else None,
                "target_logical_utterance_uid": target,
                "target_utterance_order": chronology_rank_by_logical.get(target)
                if target
                else None,
                "target_chronology_rank": chronology_rank_by_logical.get(target)
                if target
                else None,
                "target_display_utterance_order": (
                    display_utterance_order_by_logical.get(target) if target else None
                ),
                "resolution_method": reply_resolution["resolution_method"],
                "resolution_status": reply_resolution["resolution_status"],
                "resolution_confidence": reply_resolution["resolution_confidence"],
                "error_reason": reply_resolution["error_reason"],
                "reply_evidence_json": reply_resolution["reply_evidence_json"],
                "self_reference": self_reference,
                "child_before_parent": child_before_parent,
                "equal_time": bool(source_time and target_time and source_time == target_time),
                "structural_depth": None,
                "thread_root_logical_uid": None,
                "reply_lag_seconds": (
                    (source_time - target_time).total_seconds()
                    if source_time and target_time
                    else None
                ),
                "indentation": representative.get("indentation_exact"),
                "inferred_addressee": None,
                "schema_version": SCHEMA_VERSION,
            }
        )
        if self_reference:
            quality.append(
                {
                    "quality_flag_uid": _uid("wdquality", logical_uid, "reply_self_reference"),
                    "entity_uid": logical_uid,
                    "flag_code": "reply_self_reference",
                    "severity": "error",
                    "evidence_pointer": f"reply:{raw_target}",
                }
            )
        if reply_resolution["error_reason"] == "conflicting_preferred_reply_targets":
            quality.append(
                {
                    "quality_flag_uid": _uid(
                        "wdquality", logical_uid, "reply_target_evidence_conflict"
                    ),
                    "entity_uid": logical_uid,
                    "flag_code": "reply_target_evidence_conflict",
                    "severity": "error",
                    "evidence_pointer": reply_resolution["reply_evidence_json"],
                }
            )
        if child_before_parent:
            quality.append(
                {
                    "quality_flag_uid": _uid(
                        "wdquality", logical_uid, "reply_target_chronology_conflict"
                    ),
                    "entity_uid": logical_uid,
                    "flag_code": "reply_target_chronology_conflict",
                    "severity": "error",
                    "evidence_pointer": reply_resolution["reply_evidence_json"],
                }
            )

    # Derive reply depth and root only for acyclic resolved parent chains. A
    # deterministic tie-break order never substitutes for structural evidence.
    reply_by_source = {str(row["source_logical_utterance_uid"]): row for row in replies}
    for start_uid, reply in reply_by_source.items():
        seen: set[str] = set()
        node = start_uid
        depth = 0
        cycle = False
        while True:
            if node in seen:
                cycle = True
                quality.append(
                    {
                        "quality_flag_uid": _uid("wdquality", start_uid, "reply_cycle"),
                        "entity_uid": start_uid,
                        "flag_code": "reply_cycle",
                        "severity": "error",
                        "evidence_pointer": json.dumps(sorted(seen)),
                    }
                )
                break
            seen.add(node)
            parent = reply_by_source.get(node, {}).get("target_logical_utterance_uid")
            if parent is None:
                break
            depth += 1
            node = str(parent)
        if not cycle:
            reply["structural_depth"] = depth
            reply["thread_root_logical_uid"] = node

    # Reply structure and validated modification event times refine display
    # placement without changing creation-time eligibility or chronology ranks.
    # A modification must occur no earlier than creation, so the earliest valid
    # modification event is a latest-possible creation bound for unresolved rows.
    action_creation_upper_bound_by_uid: dict[str, str | None] = {}
    for logical_uid, action_rows in action_by_logical.items():
        if _parse_iso(creation_by_logical[logical_uid]["created_at"]) is not None:
            continue
        modification_times = [
            parsed
            for action in action_rows
            if action.get("action_type") == "modification"
            and (parsed := _parse_iso(action.get("event_time_utc"))) is not None
        ]
        if modification_times:
            action_creation_upper_bound_by_uid[logical_uid] = min(modification_times).isoformat()

    ordering_evidence_by_logical: dict[str, dict[str, Any]] = {}
    for logical_uids in grouped_logical.values():
        ordered_uids, ordering_evidence = _reply_constrained_display_order(
            logical_uids=logical_uids,
            creation_by_logical=creation_by_logical,
            reply_target_by_source={
                uid: reply_by_source.get(uid, {}).get("target_logical_utterance_uid")
                for uid in logical_uids
            },
            action_creation_upper_bound_by_uid=action_creation_upper_bound_by_uid,
        )
        ordering_evidence_by_logical.update(ordering_evidence)
        for display_order, logical_uid in enumerate(ordered_uids, start=1):
            display_utterance_order_by_logical[logical_uid] = display_order

    utterance_by_uid = {str(row["logical_utterance_uid"]): row for row in utterances}
    for logical_uid, utterance in utterance_by_uid.items():
        utterance["display_utterance_order"] = display_utterance_order_by_logical[logical_uid]
        existing_evidence = json.loads(str(utterance["ordering_evidence_json"]))
        existing_evidence.update(ordering_evidence_by_logical[logical_uid])
        utterance["ordering_evidence_json"] = json.dumps(existing_evidence, sort_keys=True)
    for reply in replies:
        target_uid = reply.get("target_logical_utterance_uid")
        reply["target_display_utterance_order"] = (
            display_utterance_order_by_logical.get(str(target_uid)) if target_uid else None
        )

    first_reply_by_target: dict[str, dt.datetime] = {}
    for reply in replies:
        target_uid = reply.get("target_logical_utterance_uid")
        source_uid = str(reply["source_logical_utterance_uid"])
        reply_time = _parse_iso(creation_by_logical[source_uid]["created_at"])
        if not target_uid or reply_time is None:
            continue
        current = first_reply_by_target.get(str(target_uid))
        if current is None or reply_time < current:
            first_reply_by_target[str(target_uid)] = reply_time
    link_counts = Counter(str(row["logical_utterance_uid"]) for row in links)
    signature_counts = Counter(str(row["logical_utterance_uid"]) for row in signatures)
    for utterance in utterances:
        logical_uid = str(utterance["logical_utterance_uid"])
        utterance_reply = reply_by_source.get(logical_uid)
        utterance["reply_target_logical_uid"] = (
            utterance_reply.get("target_logical_utterance_uid") if utterance_reply else None
        )
        utterance["link_count"] = link_counts[logical_uid]
        utterance["signature_count"] = signature_counts[logical_uid]
        modification_times = [
            parsed
            for action in action_by_logical[logical_uid]
            if action["action_type"] == "modification"
            and (parsed := _parse_iso(action.get("event_time_utc"))) is not None
        ]
        first_reply = first_reply_by_target.get(logical_uid)
        if first_reply is not None:
            pre_reply_representation, equal_time_uncertainty = representation_at(
                logical_uid, first_reply
            )
            utterance["pre_first_reply_representation_uid"] = (
                pre_reply_representation["representation_uid"] if pre_reply_representation else None
            )
            utterance["pre_first_reply_equal_time_uncertainty"] = equal_time_uncertainty
        utterance["modified_after_first_reply"] = (
            any(value > first_reply for value in modification_times)
            if first_reply is not None and modification_times
            else False
            if first_reply is not None
            else None
        )
        episodes_for_utterance = episode_by_conversation.get(
            str(utterance["conversation_id_exact"]), []
        )
        indexes = [
            parsed
            for episode in episodes_for_utterance
            if (parsed := _parse_iso(episode.get("episode_index_at"))) is not None
        ]
        utterance["post_cutoff_modification"] = (
            any(action_time > index for action_time in modification_times for index in indexes)
            if indexes and modification_times
            else False
            if indexes
            else None
        )

    contexts: list[dict[str, Any]] = []
    source_context_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        context_uid = source_to_context.get(str(row["source_row_uid"]))
        if context_uid:
            source_context_rows[context_uid].append(row)
    for context_uid, wc_rows in sorted(wc_context_by_uid.items()):
        ordered_wc = sorted(
            wc_rows,
            key=lambda row: (int(row["corpus_year"]), int(row["source_line_index"])),
        )
        representative = ordered_wc[0]
        lifecycle: list[dict[str, Any]] = []
        seen_context_actions: set[tuple[str, str, str]] = set()
        for wc_row in ordered_wc:
            for action in _wikiconv_lifecycle(wc_row):
                key = (
                    str(action.get("id")),
                    str(action.get("action_type")),
                    canonical_json_hash(action),
                )
                if key not in seen_context_actions:
                    seen_context_actions.add(key)
                    lifecycle.append(
                        {**action, "wikiconv_source_row_uid": wc_row["wikiconv_source_row_uid"]}
                    )
        creation = next(
            (row for row in lifecycle if row.get("action_type") == "creation"),
            lifecycle[0] if lifecycle else {},
        )
        context_created_at, _, _ = _normalize_lifecycle_event_time(
            creation.get("timestamp"), source="wikiconv_nested_lifecycle"
        )
        conversation_id = str(representative["conversation_id_exact"])
        context_source_rows = source_context_rows.get(context_uid, [])
        contexts.append(
            {
                "context_node_uid": context_uid,
                "conversation_uid": "wikiconv-conversation:" + conversation_id,
                "source_row_uid": (
                    context_source_rows[0]["source_row_uid"] if context_source_rows else None
                ),
                "source_row_uids_json": json.dumps(
                    sorted(str(row["source_row_uid"]) for row in context_source_rows)
                ),
                "context_kind": "wikiconv_section_header_or_subject",
                "text_exact": representative.get("wikiconv_text_exact"),
                "created_at_utc": context_created_at,
                "display_order": None,
                "annotation_eligible": bool(context_source_rows),
                "recovery_status": "recovered_from_pinned_wikiconv_is_section_header",
                "schema_version": SCHEMA_VERSION,
            }
        )
        for action_index, action in enumerate(lifecycle):
            action_uid = _uid(
                "wdcontextaction",
                context_uid,
                action.get("action_type"),
                action.get("id"),
                action_index,
            )
            version_uid = _uid("wdcontextversion", action_uid)
            context_meta = action.get("meta_dict")
            context_meta = context_meta if isinstance(context_meta, dict) else {}
            raw_timestamp = _iso_from_unix(action.get("timestamp"))
            event_time_utc, event_time_status, event_time_timezone = (
                _normalize_lifecycle_event_time(
                    action.get("timestamp"), source="wikiconv_nested_lifecycle"
                )
            )
            context_actions.append(
                {
                    "context_action_uid": action_uid,
                    "context_version_uid": version_uid,
                    "context_node_uid": context_uid,
                    "action_type": action.get("action_type"),
                    "action_id_exact": action.get("id"),
                    "revision_id": _revision_id(context_meta.get("rev_id"), action.get("id")),
                    "raw_timestamp": raw_timestamp,
                    "event_time_utc": event_time_utc,
                    "event_time_status": event_time_status,
                    "event_time_source": "wikiconv_nested_lifecycle",
                    "event_time_timezone": event_time_timezone,
                    "event_time_semantics": action.get("action_type"),
                    "wikiconv_source_row_uid": action.get("wikiconv_source_row_uid"),
                    "raw_action_json_canonical": json.dumps(
                        action, ensure_ascii=False, sort_keys=True, default=str
                    ),
                    "recovery_status": "recovered_from_pinned_wikiconv",
                    "schema_version": SCHEMA_VERSION,
                }
            )
            context_text = action.get("text")
            context_encoded = (context_text or "").encode("utf-8")
            context_representations.append(
                {
                    "context_representation_uid": _uid(
                        "wdcontextrepr", version_uid, "wikiconv_context_text_exact"
                    ),
                    "context_node_uid": context_uid,
                    "context_version_uid": version_uid,
                    "representation_kind": "wikiconv_context_text_exact",
                    "representation_scope": "context_node_action_field",
                    "content_sha256": sha256_bytes(context_encoded),
                    "byte_length": len(context_encoded),
                    "encoding": "utf-8",
                    "mime_type": "text/plain",
                    "content_inline": context_text,
                    "availability_status": "available" if context_text is not None else "unknown",
                    "available_at": event_time_utc,
                    "evidence_pointer": (
                        f"wikiconv_source_row:{action.get('wikiconv_source_row_uid')}"
                    ),
                    "representation_version": REPRESENTATION_VERSION,
                }
            )
        for alias_value, alias_kind in {
            (str(value), alias_kind)
            for row in ordered_wc
            for value, alias_kind in (
                (row.get("wikiconv_id_exact"), "wikiconv_current_id"),
                (row.get("ancestor_id_exact"), "wikiconv_ancestor_id"),
            )
            if value
        }:
            aliases.append(
                {
                    "alias_uid": _uid("wdalias", context_uid, alias_kind, alias_value),
                    "alias_namespace": alias_kind,
                    "alias_value_exact": alias_value,
                    "source_row_uid": None,
                    "wikiconv_source_row_uid": representative["wikiconv_source_row_uid"],
                    "entity_kind": "context_node",
                    "resolved_entity_uid": context_uid,
                    "resolution_status": "resolved",
                    "validity_status": "observed",
                    "evidence_pointer": f"context_node:{context_uid}",
                    "schema_version": SCHEMA_VERSION,
                }
            )
    existing_context_uids = {str(row["context_node_uid"]) for row in contexts}
    for context_uid, source_rows_for_context in source_context_rows.items():
        if context_uid in existing_context_uids:
            continue
        representative = min(source_rows_for_context, key=lambda row: row["source_order"])
        context_created_at, _, _ = _normalize_lifecycle_event_time(
            representative.get("wikidisputes_time"), source="wikidisputes_projection"
        )
        contexts.append(
            {
                "context_node_uid": context_uid,
                "conversation_uid": "wikiconv-conversation:"
                + str(representative["wikidisputes_conv_id_exact"]),
                "source_row_uid": representative["source_row_uid"],
                "source_row_uids_json": json.dumps(
                    sorted(str(row["source_row_uid"]) for row in source_rows_for_context)
                ),
                "context_kind": "source_only_section_header_candidate",
                "text_exact": representative.get("wikidisputes_text_exact"),
                "created_at_utc": context_created_at,
                "display_order": None,
                "annotation_eligible": True,
                "recovery_status": "source_only_unresolved_context_candidate",
                "schema_version": SCHEMA_VERSION,
            }
        )
    for context_uid, source_rows_for_context in source_context_rows.items():
        for source_row in source_rows_for_context:
            for column in (
                "wikidisputes_id_exact",
                "wikidisputes_original_id_exact",
                "wikidisputes_reply_to_exact",
            ):
                value = source_row.get(column)
                if value is None:
                    continue
                aliases.append(
                    {
                        "alias_uid": _uid(
                            "wdalias", source_row["source_row_uid"], "wikidisputes", column, value
                        ),
                        "alias_namespace": f"wikidisputes_{column}",
                        "alias_value_exact": value,
                        "source_row_uid": source_row["source_row_uid"],
                        "wikiconv_source_row_uid": None,
                        "entity_kind": "context_node",
                        "resolved_entity_uid": context_uid,
                        "resolution_status": "resolved"
                        if column != "wikidisputes_reply_to_exact"
                        else "target_alias_observed",
                        "validity_status": "observed",
                        "evidence_pointer": f"source_row:{source_row['source_row_uid']}",
                        "schema_version": SCHEMA_VERSION,
                    }
                )
    # Add stable talk-page context from exact conversation metadata alongside,
    # but distinct from, WikiConv's explicitly flagged section/title nodes.
    if metadata_path.exists():
        known_context_uids = {str(row["context_node_uid"]) for row in contexts}
        for metadata in metadata_by_conversation.values():
            conversation_id = str(metadata["conversation_id_exact"])
            conversation_uid = "wikiconv-conversation:" + conversation_id
            context_uid = _uid("wdcontext", conversation_uid, "talk_page_context")
            if context_uid in known_context_uids:
                continue
            raw_metadata = str(metadata["metadata_json_exact"])
            parsed_metadata = json.loads(raw_metadata)
            meta = parsed_metadata.get("meta", {})
            contexts.append(
                {
                    "context_node_uid": context_uid,
                    "conversation_uid": conversation_uid,
                    "source_row_uid": None,
                    "context_kind": "talk_page_context",
                    "text_exact": meta.get("page_title"),
                    "page_id_exact": meta.get("page_id"),
                    "metadata_json_exact": raw_metadata,
                    "metadata_sha256": metadata["metadata_sha256"],
                    "display_order": None,
                    "annotation_eligible": False,
                    "recovery_status": "recovered_from_pinned_wikiconv_metadata",
                    "schema_version": SCHEMA_VERSION,
                }
            )
            known_context_uids.add(context_uid)
    display: list[dict[str, Any]] = []
    context_by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for context in contexts:
        context_by_conversation[str(context["conversation_uid"])].append(context)
    utterance_by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for utterance in utterances:
        utterance_by_conversation[str(utterance["conversation_uid"])].append(utterance)
    for conversation_uid in sorted(set(context_by_conversation) | set(utterance_by_conversation)):
        entries: list[tuple[str, dict[str, Any]]] = [
            *(("context", row) for row in context_by_conversation[conversation_uid]),
            *(("utterance", row) for row in utterance_by_conversation[conversation_uid]),
        ]

        def display_key(entry: tuple[str, dict[str, Any]]) -> tuple[Any, ...]:
            kind, row = entry
            # Context remains descriptive scaffolding and does not participate
            # in inferred utterance chronology. Keep it ahead of the thread in
            # a deterministic order, then use the reply-constrained ordering
            # for every logical utterance, including unknown-time rows.
            if kind == "context":
                return (
                    0,
                    0 if row.get("context_kind") == "talk_page_context" else 1,
                    _parse_iso(row.get("created_at_utc")) or dt.datetime.min.replace(tzinfo=dt.UTC),
                    row["context_node_uid"],
                )
            return (
                1,
                int(row["display_utterance_order"]),
                0,
                row["logical_utterance_uid"],
            )

        for position, (kind, row) in enumerate(sorted(entries, key=display_key), start=1):
            if kind == "context":
                row["display_order"] = position
                display.append(
                    {
                        "display_row_uid": _uid("wddisplay", row["context_node_uid"]),
                        "conversation_uid": conversation_uid,
                        "row_kind": "context",
                        "context_node_uid": row["context_node_uid"],
                        "logical_utterance_uid": None,
                        "display_order": position,
                        "utterance_order": None,
                        "chronology_eligible": False,
                        "chronology_status": "context_row",
                        "chronology_rank": None,
                        "annotation_eligible": bool(row.get("source_row_uid")),
                        "text_exact": row.get("text_exact"),
                    }
                )
                continue
            utterance = row
            logical_uid = str(utterance["logical_utterance_uid"])
            utterance["display_order"] = position
            preferred_representation_uids = [
                utterance.get("final_text_representation_uid"),
                utterance.get("wikidisputes_text_representation_uid"),
                utterance.get("creation_text_representation_uid"),
            ]

            text_repr = None

            # Prefer the first NONEMPTY representation in canonical
            # representation priority.
            for representation_uid in preferred_representation_uids:
                if not representation_uid:
                    continue

                candidate = next(
                    (
                        representation
                        for representation in representation_by_logical[logical_uid]
                        if representation.get("representation_uid") == representation_uid
                    ),
                    None,
                )

                if candidate is None:
                    continue

                content = candidate.get("content_inline")

                if isinstance(content, str) and content.strip():
                    text_repr = candidate
                    break

            display_text = text_repr.get("content_inline") if text_repr else None

            # Absolute final safeguard: exact WikiDisputes source text can
            # never be replaced by an empty reconstructed representation.
            if not isinstance(display_text, str) or not display_text.strip():
                exact_source_text = utterance.get("wikidisputes_text_exact")

                if isinstance(exact_source_text, str) and exact_source_text.strip():
                    display_text = exact_source_text

            display.append(
                {
                    "display_row_uid": _uid("wddisplay", logical_uid),
                    "conversation_uid": conversation_uid,
                    "row_kind": "utterance",
                    "context_node_uid": None,
                    "logical_utterance_uid": logical_uid,
                    "display_order": position,
                    "utterance_order": utterance["utterance_order"],
                    "chronology_eligible": utterance["chronology_eligible"],
                    "chronology_status": utterance["chronology_status"],
                    "chronology_rank": utterance["chronology_rank"],
                    "annotation_eligible": True,
                    "text_exact": display_text,
                }
            )

    episode_memberships: list[dict[str, Any]] = []
    cutoff_representations_by_logical: dict[str, set[str]] = defaultdict(set)
    for utterance in utterances:
        conversation_id = str(utterance["conversation_id_exact"])
        for episode in episode_by_conversation.get(conversation_id, []):
            episode_uid = str(episode["episode_uid"])
            index = _parse_iso(episode.get("episode_index_at"))
            created_at = _parse_iso(utterance.get("created_at_utc"))
            in_episode_window = bool(index and created_at and created_at <= index)
            cutoff_representation, equal_time_uncertainty = (
                representation_at(str(utterance["logical_utterance_uid"]), index)
                if index
                else (None, False)
            )
            predictor_eligible = bool(
                in_episode_window
                and cutoff_representation
                and cutoff_representation.get("availability_status")
                not in {"deleted_at_action", "unavailable", "hidden", "suppressed"}
            )
            outcome_eligible = bool(
                in_episode_window
                and str(episode.get("analysis_status", "")).startswith("eligible_")
            )
            if cutoff_representation:
                cutoff_representations_by_logical[str(utterance["logical_utterance_uid"])].add(
                    str(cutoff_representation["representation_uid"])
                )
            utterance["in_episode_window"] = bool(
                utterance["in_episode_window"] or in_episode_window
            )
            utterance["predictor_eligible"] = bool(
                utterance["predictor_eligible"] or predictor_eligible
            )
            utterance["outcome_eligible"] = bool(utterance["outcome_eligible"] or outcome_eligible)
            episode_memberships.append(
                {
                    "episode_uid": episode_uid,
                    "logical_utterance_uid": utterance["logical_utterance_uid"],
                    "membership_uid": _uid(
                        "wdepisode-utterance", episode_uid, utterance["logical_utterance_uid"]
                    ),
                    "source_wikidisputes_escalated": episode["source_wikidisputes_escalated"],
                    "episode_index_at": episode.get("episode_index_at"),
                    "cutoff_rule_version": episode.get("cutoff_rule_version"),
                    "in_episode_window": in_episode_window,
                    "predictor_eligible": predictor_eligible,
                    "outcome_eligible": outcome_eligible,
                    "predictor_cutoff_representation_uid": (
                        cutoff_representation["representation_uid"]
                        if cutoff_representation
                        else None
                    ),
                    "predictor_cutoff_content_sha256": (
                        cutoff_representation.get("content_sha256")
                        if cutoff_representation
                        else None
                    ),
                    "predictor_representation_available_at": (
                        cutoff_representation.get("available_at") if cutoff_representation else None
                    ),
                    "predictor_representation_leakage_class": (
                        "at_or_before_episode_index"
                        if cutoff_representation
                        else "historical_state_unavailable"
                    ),
                    "equal_time_action_uncertainty": equal_time_uncertainty,
                    "predictor_selection_rule_version": (
                        "historical-lifecycle-at-or-before-cutoff-v1"
                    ),
                    "analysis_status": episode.get("analysis_status"),
                    "analysis_rule_version": episode.get("analysis_rule_version"),
                    "split_group_episode_uid": episode_uid,
                    "split_group_thread_uids_json": json.dumps(
                        sorted(set(thread_uids_by_episode.get(episode_uid, [])))
                    ),
                    "split_group_article_page_id": episode.get("page_id_exact"),
                    "split_group_conversation_uid": utterance["conversation_uid"],
                    "split_group_participant_alias_keys_json": json.dumps(
                        participant_split_keys_by_episode.get(episode_uid, [])
                    ),
                    "dv_values_json": json.dumps(
                        outcomes_by_episode.get(episode_uid, []),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    "schema_version": SCHEMA_VERSION,
                }
            )
    for utterance in utterances:
        candidates = cutoff_representations_by_logical.get(
            str(utterance["logical_utterance_uid"]), set()
        )
        utterance["predictor_cutoff_representation_uid"] = (
            next(iter(candidates)) if len(candidates) == 1 else None
        )

    # Refresh the future-annotation join contract with the authoritative full
    # identity resolution. Exact source fields and source hashes remain copied
    # from the immutable projection; no annotation or Gold data is consulted.
    old_join = _read_parquet_rows(output_root / "silver" / "annotation_join_contract.parquet")
    utterance_by_uid = {str(row["logical_utterance_uid"]): row for row in utterances}
    context_uid_by_source = {
        str(row["source_row_uid"]): str(row["context_node_uid"])
        for row in contexts
        if row.get("source_row_uid")
    }
    context_uid_by_source.update(source_to_context)
    action_by_source = source_action_resolution
    display_order_by_uid = {
        str(row.get("logical_utterance_uid") or row.get("context_node_uid")): row["display_order"]
        for row in display
    }
    reply_target_by_uid = {
        str(row["source_logical_utterance_uid"]): row["target_logical_utterance_uid"]
        for row in replies
    }
    refreshed_join: list[dict[str, Any]] = []
    for row in old_join:
        source_uid = str(row["source_row_uid"])
        resolved_logical_uid = source_to_logical.get(source_uid)
        resolved_context_uid = context_uid_by_source.get(source_uid)
        source_action = action_by_source.get(source_uid)
        resolved_utterance = (
            utterance_by_uid.get(resolved_logical_uid) if resolved_logical_uid else None
        )
        row.update(
            {
                "logical_utterance_uid": resolved_logical_uid,
                "context_node_uid": resolved_context_uid,
                "version_uid": source_action["version_uid"] if source_action else None,
                "action_uid": source_action["action_uid"] if source_action else None,
                "utterance_order": (
                    resolved_utterance["utterance_order"] if resolved_utterance else None
                ),
                "chronology_eligible": (
                    resolved_utterance["chronology_eligible"] if resolved_utterance else False
                ),
                "chronology_status": (
                    resolved_utterance["chronology_status"] if resolved_utterance else "context_row"
                ),
                "chronology_rank": (
                    resolved_utterance["chronology_rank"] if resolved_utterance else None
                ),
                # Every immutable source occurrence remains available for
                # coding.  Context is descriptive provenance, not an
                # exclusion criterion and not a reason to fabricate a logical
                # utterance identity.
                "annotation_eligible": True,
                "display_order": display_order_by_uid.get(
                    resolved_logical_uid or resolved_context_uid or ""
                ),
                "reply_target_logical_uid": reply_target_by_uid.get(resolved_logical_uid or ""),
                "identity_algorithm_version": IDENTITY_VERSION,
                "join_contract_version": JOIN_CONTRACT_VERSION,
            }
        )
        refreshed_join.append(row)

    context_join_contract = [
        {
            "context_node_uid": context["context_node_uid"],
            "conversation_uid": context["conversation_uid"],
            "source_row_uid": context.get("source_row_uid"),
            "context_kind": context.get("context_kind"),
            "text_exact": context.get("text_exact"),
            "display_order": display_order_by_uid.get(str(context["context_node_uid"])),
            "annotation_eligible": bool(context.get("source_row_uid")),
            "schema_version": SCHEMA_VERSION,
            "identity_algorithm_version": IDENTITY_VERSION,
            "join_contract_version": JOIN_CONTRACT_VERSION,
        }
        for context in contexts
    ]

    old_registry_by_anchor = {
        str(row["selected_anchor"]): row
        for row in existing_registry
        if row.get("selected_anchor") is not None
    }
    for row in registry:
        prior = old_registry_by_anchor.get(str(row.get("selected_anchor")))
        if prior and prior.get("issued_uid") != row.get("issued_uid"):
            aliases.append(
                {
                    "alias_uid": _uid(
                        "wdalias",
                        "identity_redirect",
                        prior["issued_uid"],
                        row["issued_uid"],
                        SCHEMA_VERSION,
                    ),
                    "alias_namespace": "identity_registry_redirect",
                    "alias_value_exact": prior["issued_uid"],
                    "source_row_uid": None,
                    "wikiconv_source_row_uid": None,
                    "entity_kind": "identity_redirect",
                    "resolved_entity_uid": row["issued_uid"],
                    "resolution_status": "redirect_effective",
                    "validity_status": "append_only_historical_alias",
                    "effective_version": SCHEMA_VERSION,
                    "evidence_pointer": f"registry_entry:{row['registry_entry_uid']}",
                    "schema_version": SCHEMA_VERSION,
                }
            )
    registry = _append_only_registry(existing_registry, registry)

    counts = {
        "source_rows": len(source),
        "wikiconv_rows_before_identical_observation_dedup": len(wikiconv_all),
        "wikiconv_observations": len(wikiconv),
        "logical_utterances": len(utterances),
        "source_logical_utterances": sum(
            bool(row["in_wikidisputes_release"]) for row in utterances
        ),
        "additional_rehydrated_utterances": sum(
            bool(row["additional_rehydrated_absent_from_wikidisputes"]) for row in utterances
        ),
        "context_nodes": len(contexts),
        "context_actions": len(context_actions),
        "context_representations": len(context_representations),
        "actions": len(actions),
        "modifications": sum(row["action_type"] == "modification" for row in actions),
        "deletions": sum(row["action_type"] == "deletion" for row in actions),
        "restorations": sum(row["action_type"] == "restoration" for row in actions),
        "source_only_unresolved_logical": sum(
            row["recovery_status"] == "source_only_unresolved" for row in utterances
        ),
        "unavailable_or_suppressed_actions": sum(
            row.get("recovery_status") in {"unavailable", "hidden", "suppressed"} for row in actions
        ),
        "episode_memberships": len(episode_memberships),
        "signatures": len(signatures),
        "links": len(links),
        "chronology_eligible_utterances": sum(
            bool(row["chronology_eligible"]) for row in utterances
        ),
        "chronology_unresolved_utterances": sum(
            not bool(row["chronology_eligible"]) for row in utterances
        ),
    }

    # All derivations are complete. Drop input rows and secondary indexes before
    # Arrow serialization so their Python object graphs do not overlap with the
    # output buffers. The output lists below remain the sole owners of emitted
    # row mappings and are cleared immediately after each atomic write.
    del (
        action_by_source,
        context_by_conversation,
        context_source_rows,
        context_uid_by_source,
        cutoff_representations_by_logical,
        dispute_by_uid,
        disputes,
        display_order_by_uid,
        existing_registry,
        first_reply_by_target,
        grouped_logical,
        metadata_by_conversation,
        metadata_candidates,
        old_join,
        outcome_rows,
        outcomes_by_episode,
        reply_by_source,
        reply_target_by_uid,
        representation_by_logical,
        source,
        source_action_resolution,
        source_by_logical,
        source_identity,
        source_to_context,
        source_to_logical,
        substantive_source_rows,
        utterance_by_conversation,
        utterance_by_uid,
        wc_alias_to_logical,
        wc_by_logical,
        wc_by_observation,
        wc_context_alias_to_uid,
        wc_context_by_uid,
        wc_utterance_rows,
        wikiconv,
        wikiconv_all,
    )
    gc.collect()

    artifacts: dict[str, Any] = {}
    silver = output_root / "silver"
    canonical = output_root / "canonical"

    def write_artifact(name: str, artifact_rows: list[dict[str, Any]]) -> None:
        artifacts[name] = _write(silver / f"{name}.parquet", artifact_rows)
        artifact_rows.clear()
        gc.collect()

    chronology_strict = sorted(
        (row for row in utterances if row["chronology_eligible"]),
        key=lambda row: (
            str(row["conversation_uid"]),
            int(row["chronology_rank"]),
            str(row["logical_utterance_uid"]),
        ),
    )
    strict_path = canonical / "wikidisputes_chronology_strict.parquet"
    artifacts["wikidisputes_chronology_strict"] = _write(strict_path, chronology_strict)
    chronology_strict.clear()
    gc.collect()

    write_artifact("utterance_representations", representations)
    write_artifact("source_id_aliases", aliases)
    write_artifact("utterance_actions", actions)
    versions_path = silver / "utterance_versions.parquet"
    atomic_link_or_copy(silver / "utterance_actions.parquet", versions_path)
    artifacts["utterance_versions"] = {
        **file_descriptor(versions_path),
        "rows": counts["actions"],
    }
    write_artifact("utterances", utterances)
    write_artifact("episode_utterances", episode_memberships)
    write_artifact("annotation_join_contract", refreshed_join)
    write_artifact("identity_registry", registry)
    write_artifact("signatures", signatures)
    write_artifact("authors_actors", actor_rows)
    write_artifact("reply_edges", replies)
    write_artifact("context_actions", context_actions)
    write_artifact("context_representations", context_representations)
    write_artifact("context_nodes", contexts)
    write_artifact("annotation_context_join_contract", context_join_contract)
    write_artifact("links", links)
    write_artifact("quality_flags", quality)
    write_artifact("dispute_episodes", episode_rows)

    for export_name, silver_name, row_count in (
        ("wikidisputes_utterances_ssot", "utterances", counts["logical_utterances"]),
        (
            "wikidisputes_episode_utterances_ssot",
            "episode_utterances",
            counts["episode_memberships"],
        ),
    ):
        target = canonical / f"{export_name}.parquet"
        atomic_link_or_copy(silver / f"{silver_name}.parquet", target)
        artifacts[export_name] = {**file_descriptor(target), "rows": row_count}
    artifacts["wikidisputes_annotation_display"] = _write(
        canonical / "wikidisputes_annotation_display.parquet", display
    )
    display.clear()
    gc.collect()

    report = {
        "status": enumeration_report["status"],
        "schema_version": SCHEMA_VERSION,
        "identity_algorithm_version": IDENTITY_VERSION,
        "chronology_algorithm_version": CHRONOLOGY_VERSION,
        "join_contract_version": JOIN_CONTRACT_VERSION,
        "conversational_completeness_claim": enumeration_report["status"] == "complete",
        "counts": counts,
        "enumeration": enumeration_report,
        "artifacts": artifacts,
        "chronology_relevant_artifact_hashes": {
            name: artifacts[name]["sha256"]
            for name in (
                "utterances",
                "reply_edges",
                "annotation_join_contract",
                "wikidisputes_chronology_strict",
                "wikidisputes_annotation_display",
            )
            if name in artifacts
        },
    }
    report["cross_label_reconciliation"] = materialize_cross_label_reconciliation(output_root)
    atomic_write_json(output_root / "reports" / "full_rehydration.json", report)
    return report
