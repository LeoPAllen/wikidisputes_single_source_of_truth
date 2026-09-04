from __future__ import annotations

import datetime as dt
import difflib
import gzip
import html
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

import mwparserfromhell
import pyarrow as pa
import pyarrow.parquet as pq

from .config import Settings
from .constants import REPRESENTATION_VERSION
from .hashing import canonical_json_hash, sha256_bytes
from .io import atomic_link_or_copy, atomic_parquet, atomic_write_json, file_descriptor
from .representations import extract_links, extract_signature_evidence


def _visible(wikitext: str) -> str:
    return mwparserfromhell.parse(wikitext).strip_code(normalize=False, collapse=False)


def _normalized(value: str) -> str:
    return " ".join(html.unescape(value).split()).casefold()


def _signature_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return (
            dt.datetime.strptime(value, "%H:%M, %d %B %Y (UTC)").replace(tzinfo=dt.UTC).isoformat()
        )
    except ValueError:
        return None


def extract_fragment(
    revision_wikitext: str, visible_text: str, action_id: str | None
) -> dict[str, Any]:
    """Conservatively extract a comment fragment with explicit evidence/status."""
    exact_start = revision_wikitext.find(visible_text)
    if visible_text and exact_start >= 0:
        end = exact_start + len(visible_text)
        return {
            "fragment": revision_wikitext[exact_start:end],
            "character_start": exact_start,
            "character_end": end,
            "method": "exact_visible_text_in_revision_wikitext",
            "confidence": "high",
            "status": "recovered",
        }
    positions: list[int] = []
    if action_id:
        for part in action_id.split(".")[1:3]:
            with suppress(ValueError):
                positions.append(int(part))
    best: tuple[float, int, int, str] | None = None
    expected_length = max(len(visible_text), 80)
    for start in sorted(set(positions)):
        if start < 0 or start >= len(revision_wikitext):
            continue
        limit = min(len(revision_wikitext), start + expected_length * 4 + 1500)
        window = revision_wikitext[start:limit]
        signature = re.search(r"\d{1,2}:\d{2},\s+\d{1,2}\s+[A-Z][a-z]+\s+\d{4}\s+\(UTC\)", window)
        if signature:
            relative_end = signature.end()
        else:
            minimum = max(1, expected_length // 3)
            boundary = re.search(r"\n(?:(?:==+)|\n)", window[minimum:])
            relative_end = minimum + boundary.start() if boundary else len(window)
        fragment = window[:relative_end]
        similarity = difflib.SequenceMatcher(
            None, _normalized(visible_text), _normalized(_visible(fragment)), autojunk=False
        ).ratio()
        candidate = (similarity, start, start + relative_end, fragment)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best and best[0] >= 0.60:
        return {
            "fragment": best[3],
            "character_start": best[1],
            "character_end": best[2],
            "method": "wikiconv_id_position_with_visible_similarity_v1",
            "confidence": f"similarity:{best[0]:.6f}",
            "status": "recovered_candidate",
        }
    return {
        "fragment": None,
        "character_start": None,
        "character_end": None,
        "method": "exact_and_position_candidates_exhausted_v1",
        "confidence": "none",
        "status": "unresolved",
    }


def _iter_revision_content(settings: Settings, blob_paths: set[str]) -> Iterator[tuple[str, str]]:
    """Yield revision wikitext one response blob at a time to bound memory."""
    for relative in sorted(blob_paths):
        path = settings.roots.data / "bronze" / "blobs" / relative
        payload = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        response = json.loads(payload)
        pages = response.get("query", {}).get("pages", [])
        if not isinstance(pages, list):
            continue
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("revisions"), list):
                continue
            for revision in page["revisions"]:
                if not isinstance(revision, dict):
                    continue
                slots = revision.get("slots")
                main = slots.get("main") if isinstance(slots, dict) else None
                text = main.get("content", main.get("*")) if isinstance(main, dict) else None
                if text is None:
                    text = revision.get("content", revision.get("*"))
                if revision.get("revid") is not None and isinstance(text, str):
                    yield str(revision["revid"]), text


def _iter_actions_with_content(
    settings: Settings,
    blob_paths: set[str],
    actions_by_revision: dict[str, list[dict[str, Any]]],
) -> Iterator[tuple[dict[str, Any], str]]:
    for revision_id, content in _iter_revision_content(settings, blob_paths):
        for action in actions_by_revision.get(revision_id, []):
            yield action, content


def _union_table(rows: list[dict[str, Any]]) -> pa.Table:
    columns = sorted({column for row in rows for column in row})
    return pa.Table.from_pylist([{column: row.get(column) for column in columns} for row in rows])


def _signature_actor_match_status(
    signature: dict[str, Any], actor_observation: dict[str, Any]
) -> str:
    actor = actor_observation.get("actor_name_exact")
    targets = {
        _normalized(str(value))
        for value in (
            signature.get("user_target"),
            signature.get("user_talk_target"),
            signature.get("contributions_target"),
        )
        if value
    }
    if actor_observation.get("userhidden"):
        return "revision_actor_hidden_or_deleted"
    if not actor:
        return "revision_actor_not_observed"
    if not targets:
        return "signature_identity_target_not_observed"
    if _normalized(str(actor)) in targets:
        return "exact_normalized_target_match"
    return "observed_mismatch_or_rename"


def recover_revision_representations(settings: Settings) -> dict[str, Any]:
    silver = settings.roots.output / "silver"
    observations = pq.read_table(silver / "talk_page_revision_observations.parquet").to_pylist()
    blob_paths = {
        str(row["response_blob_path"]) for row in observations if row.get("response_blob_path")
    }
    actions = pq.read_table(silver / "utterance_actions.parquet").to_pylist()
    actions_by_revision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        if action.get("revision_id") is not None:
            actions_by_revision[str(action["revision_id"])].append(action)
    actor_observation_by_revision = {
        str(row["revision_id"]): row for row in observations if row.get("revision_id") is not None
    }
    representations = pq.read_table(silver / "utterance_representations.parquet").to_pylist()
    representation_by_version: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for representation in representations:
        representation_by_version[str(representation["version_uid"])].append(representation)
    recovered_representations: list[dict[str, Any]] = []
    recovered_links: list[dict[str, Any]] = []
    recovered_signatures: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    revision_content_ids: set[str] = set()
    for action, raw_wikitext in _iter_actions_with_content(
        settings, blob_paths, actions_by_revision
    ):
        revision_id = str(action.get("revision_id"))
        revision_content_ids.add(revision_id)
        version_uid = str(action["version_uid"])
        logical_uid = str(action["logical_utterance_uid"])
        visible_source = next(
            (
                row.get("content_inline")
                for row in representation_by_version.get(version_uid, [])
                if row.get("representation_kind")
                in {"wikiconv_action_text_exact", "wikidisputes_text_exact"}
                and isinstance(row.get("content_inline"), str)
            ),
            "",
        )
        result = extract_fragment(
            raw_wikitext, str(visible_source), str(action.get("action_id_exact") or "")
        )
        statuses[result["status"]] += 1
        if result["fragment"] is None:
            continue
        fragment = str(result["fragment"])
        fragment_uid = "wdrepr:v1:" + canonical_json_hash(
            [version_uid, revision_id, "utterance_wikitext_fragment", result["character_start"]]
        )
        reconstructed_visible = _visible(fragment)
        visible_uid = "wdrepr:v1:" + canonical_json_hash(
            [fragment_uid, "visible_text_reconstructed"]
        )
        common = {
            "logical_utterance_uid": logical_uid,
            "version_uid": version_uid,
            "source_row_uid": action.get("source_row_uid"),
            "source_revision_id": revision_id,
            "extraction_method": result["method"],
            "extraction_version": "1.0.0",
            "availability_status": result["status"],
            "leakage_class": "historical_revision_state",
            "available_at": action.get("raw_timestamp"),
            "confidence": result["confidence"],
            "representation_version": REPRESENTATION_VERSION,
        }
        recovered_representations.extend(
            [
                {
                    **common,
                    "representation_uid": fragment_uid,
                    "representation_kind": "utterance_wikitext_fragment",
                    "representation_scope": "logical_utterance_fragment",
                    "content_sha256": sha256_bytes(fragment.encode("utf-8")),
                    "byte_length": len(fragment.encode("utf-8")),
                    "encoding": "utf-8",
                    "mime_type": "text/x-wiki",
                    "content_inline": fragment,
                    "blob_path": None,
                    "character_start": result["character_start"],
                    "character_end": result["character_end"],
                },
                {
                    **common,
                    "representation_uid": visible_uid,
                    "representation_kind": "visible_text_reconstructed",
                    "representation_scope": "logical_utterance_fragment",
                    "content_sha256": sha256_bytes(reconstructed_visible.encode("utf-8")),
                    "byte_length": len(reconstructed_visible.encode("utf-8")),
                    "encoding": "utf-8",
                    "mime_type": "text/plain",
                    "content_inline": reconstructed_visible,
                    "blob_path": None,
                    "character_start": None,
                    "character_end": None,
                },
            ]
        )
        links = extract_links(fragment, logical_utterance_uid=logical_uid, version_uid=version_uid)
        for link in links:
            recovered_links.append(
                {
                    **link.__dict__,
                    "logical_utterance_uid": logical_uid,
                    "version_uid": version_uid,
                    "source_representation_uid": fragment_uid,
                    "present_in_wikidisputes_text": bool(
                        link.raw_target and link.raw_target in str(visible_source)
                    ),
                    "recovered_from_revision": True,
                    "evidence_pointer": f"representation:{fragment_uid}",
                    "confidence": result["confidence"],
                    "ambiguity": None,
                }
            )
        signature = extract_signature_evidence(fragment)
        actor_observation = actor_observation_by_revision.get(revision_id, {})
        actor = actor_observation.get("actor_name_exact")
        actor_match_status = _signature_actor_match_status(signature, actor_observation)
        recovered_signatures.append(
            {
                "signature_uid": "wdsignature:v1:"
                + canonical_json_hash([version_uid, fragment_uid]),
                "logical_utterance_uid": logical_uid,
                "version_uid": version_uid,
                **signature,
                "signature_html_reconstructed": None,
                "parsed_signature_timestamp": _signature_time(
                    signature.get("raw_signature_timestamp_text")
                ),
                "revision_actor_name_exact": actor,
                "revision_actor_user_id": actor_observation.get("actor_user_id"),
                "actor_match_status": actor_match_status,
                "evidence_pointer": f"representation:{fragment_uid}",
                "confidence": result["confidence"],
            }
        )
        if str(visible_source) == reconstructed_visible:
            category = "exact_visible_match"
        elif _normalized(str(visible_source)) == _normalized(reconstructed_visible):
            category = "whitespace_or_entity_difference"
        elif links:
            category = "markup_or_link_target_loss_or_version_difference"
        else:
            category = "comment_boundary_or_version_difference"
        discrepancies.append(
            {
                "discrepancy_uid": "wddiscrepancy:v1:"
                + canonical_json_hash([version_uid, fragment_uid, category]),
                "logical_utterance_uid": logical_uid,
                "version_uid": version_uid,
                "category": category,
                "source_visible_sha256": sha256_bytes(str(visible_source).encode("utf-8")),
                "reconstructed_visible_sha256": sha256_bytes(reconstructed_visible.encode("utf-8")),
                "evidence_pointer": f"representation:{fragment_uid}",
            }
        )

    representation_rows = list(
        {
            str(row["representation_uid"]): row
            for row in representations + recovered_representations
        }.values()
    )
    atomic_parquet(silver / "utterance_representations.parquet", _union_table(representation_rows))
    existing_links = pq.read_table(silver / "links.parquet").to_pylist()
    if existing_links and "link_uid" not in existing_links[0]:
        existing_links = []
    link_rows = list(
        {str(row["link_uid"]): row for row in existing_links + recovered_links}.values()
    )
    atomic_parquet(
        silver / "links.parquet",
        _union_table(link_rows) if link_rows else pa.table({"_empty": pa.array([], pa.string())}),
    )
    existing_signatures = pq.read_table(silver / "signatures.parquet").to_pylist()
    signature_rows = list(
        {
            str(row["signature_uid"]): row
            for row in existing_signatures + recovered_signatures
            if row.get("signature_uid")
        }.values()
    )
    atomic_parquet(
        silver / "signatures.parquet",
        _union_table(signature_rows)
        if signature_rows
        else pa.table({"_empty": pa.array([], pa.string())}),
    )
    creation_version = {
        str(row["logical_utterance_uid"]): str(row["version_uid"])
        for row in actions
        if row.get("action_type") == "creation"
    }
    representation_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sorted(representation_rows, key=lambda item: str(item["representation_uid"])):
        logical_uid = str(row["logical_utterance_uid"])
        if str(row.get("version_uid")) != creation_version.get(logical_uid):
            continue
        representation_lookup.setdefault((logical_uid, str(row["representation_kind"])), row)
    link_counts = Counter(str(row["logical_utterance_uid"]) for row in link_rows)
    signature_counts = Counter(str(row["logical_utterance_uid"]) for row in signature_rows)
    utterance_path = silver / "utterances.parquet"
    utterances = pq.read_table(utterance_path).to_pylist()
    for utterance in utterances:
        logical_uid = str(utterance["logical_utterance_uid"])
        for kind, column in (
            ("revision_wikitext_raw", "revision_wikitext_representation_uid"),
            ("utterance_wikitext_fragment", "utterance_wikitext_fragment_representation_uid"),
            ("visible_text_reconstructed", "visible_text_reconstructed_representation_uid"),
        ):
            representation = representation_lookup.get((logical_uid, kind))
            utterance[column] = representation["representation_uid"] if representation else None
        utterance["link_count"] = link_counts[logical_uid]
        utterance["signature_count"] = signature_counts[logical_uid]
    atomic_parquet(utterance_path, _union_table(utterances))
    atomic_link_or_copy(
        utterance_path,
        settings.roots.output / "canonical" / "wikidisputes_utterances_ssot.parquet",
    )
    discrepancy_path = settings.roots.output / "reports" / "representation_discrepancies.parquet"
    atomic_parquet(
        discrepancy_path,
        _union_table(discrepancies)
        if discrepancies
        else pa.table({"_empty": pa.array([], pa.string())}),
    )
    report = {
        "revision_content_count": len(revision_content_ids),
        "fragment_status_counts": dict(statuses),
        "fragment_representations_added": len(recovered_representations) // 2,
        "visible_representations_added": len(recovered_representations) // 2,
        "revision_links_added": len(recovered_links),
        "revision_signatures_added": len(recovered_signatures),
        "discrepancy_counts": dict(Counter(row["category"] for row in discrepancies)),
        "discrepancy_artifact": {
            **file_descriptor(discrepancy_path),
            "rows": len(discrepancies),
        },
        "rendered_html_reconstructed_count": 0,
        "html_archival_count": 0,
    }
    atomic_write_json(settings.roots.output / "reports" / "representation_recovery.json", report)
    return report
