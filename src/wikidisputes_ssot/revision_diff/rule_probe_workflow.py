"""Read-only orchestration for deterministic residual rule probes."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from wikidisputes_ssot.config import Settings
from wikidisputes_ssot.hashing import canonical_json_hash
from wikidisputes_ssot.io import (
    atomic_parquet,
    atomic_write_bytes,
    atomic_write_json,
    file_descriptor,
    table_from_union_pylist,
)

from .cache import hydrate_revision_pairs
from .llm_audit_bundle import LLMAuditBundlePaths
from .residual_ceiling import SEED
from .residual_ceiling_workflow import ResidualCeilingPaths
from .rule_probes import run_probes, summarize_probe_results
from .workflow import MethodBPaths

FROZEN_SAMPLE_SIZE = 600
FROZEN_SAMPLE_UID_HASH = "5135ac57e21a59f7b0e68ea26eef2508420eec89f7cc822839ddea438916b5d5"
FROZEN_SIGNATURE_FRAGMENT_UIDS = (
    "wdrow:v1:19313c42c31341d68dce73151154402cb642a336e9d810cd3c26b49c8c5a021a",
    "wdrow:v1:34e5ab2aae8ff592634a53e6f4051a7d628745ff240d9a2b1fd8fd4a27b68a13",
    "wdrow:v1:3768bb8db08d53cd3052ef9e89004448ce11bf7ee8786157cd5a62bbfacb73f8",
    "wdrow:v1:e6bd8dcd11b74138b556bdd20b130a0b7f379ce3fbf215fd38d6464d2672c53f",
)


@dataclass(frozen=True, slots=True)
class RuleProbePaths:
    root: Path
    rows: Path
    summary: Path
    signature_fragment_diagnostics: Path

    @classmethod
    def from_settings(cls, settings: Settings, seed: str = SEED) -> RuleProbePaths:
        root = ResidualCeilingPaths.from_settings(settings, seed).root / "rule_probes"
        return cls(
            root=root,
            rows=root / "frozen_600_rule_probe_results.parquet",
            summary=root / "frozen_600_rule_probe_summary.json",
            signature_fragment_diagnostics=root / "x1_signature_fragment_diagnostics.jsonl",
        )


def _signature_fragment_role(extra_suffix: str) -> tuple[str, str]:
    user_link = re.search(r"\[\[\s*User(?:[ _]+talk)?\s*:", extra_suffix, re.I)
    if user_link:
        before_link = re.sub(r"</?[^>]+>", "", extra_suffix[: user_link.start()])
        if not re.sub(r"[\s:;#*()'\"`.,~\-/\u2013\u2014]+", "", before_link):
            return (
                "historical_author_signature_fragment",
                "body suffix is only signature wrapper/user markup before parsed signature",
            )
    return (
        "substantive_comment_text",
        "body suffix contains substantive prose beyond the frozen source",
    )


def _build_signature_fragment_diagnostics(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_uid = {str(row["source_row_uid"]): row for row in rows}
    if not set(FROZEN_SIGNATURE_FRAGMENT_UIDS) <= set(by_uid):
        raise RuntimeError("frozen signature-fragment UIDs are absent from probe input")
    diagnostics: list[dict[str, Any]] = []
    for uid in FROZEN_SIGNATURE_FRAGMENT_UIDS:
        row = by_uid[uid]
        source = str(row["source_text"])
        source_span = row["source_match_spans"][0]
        containing = [
            candidate
            for candidate in row.get("all_candidates") or []
            if int(candidate["start"]) <= int(source_span["start"])
            and int(source_span["end"]) <= int(candidate["end"])
        ]
        if len(containing) != 1:
            raise RuntimeError(f"signature diagnostic candidate is not unique: {uid}")
        candidate = containing[0]
        body = str(candidate["body_wikitext"])
        local_start = int(source_span["start"]) - int(candidate["body_start"])
        local_end = local_start + len(source)
        if body[local_start:local_end] != source:
            raise RuntimeError(f"source/body diagnostic offset mismatch: {uid}")
        extra_prefix, extra_suffix = body[:local_start], body[local_end:]
        role, reason = _signature_fragment_role(extra_suffix)
        diagnostics.append(
            {
                "source_row_uid": uid,
                "source": source,
                "candidate_uid": candidate["candidate_uid"],
                "candidate_body": body,
                "candidate_raw": candidate["raw_wikitext"],
                "parsed_signature": candidate.get("raw_signature_wikitext"),
                "body_start": candidate["body_start"],
                "body_end": candidate["body_end"],
                "signature_start": candidate.get("signature_start"),
                "signature_end": candidate.get("signature_end"),
                "frozen_speaker": row.get("wikiconv_speaker"),
                "signature_user": candidate.get("signature_user_target"),
                "boundary_evidence": candidate.get("boundary_evidence") or [],
                "extra_prefix": extra_prefix,
                "extra_suffix": extra_suffix,
                "fragment_role": role,
                "classification": reason,
            }
        )
    roles: dict[str, int] = {}
    for row in diagnostics:
        role = str(row["fragment_role"])
        roles[role] = roles.get(role, 0) + 1
    return diagnostics, {
        "rows": len(diagnostics),
        "fragment_role_counts": dict(sorted(roles.items())),
        "single_generalizable_rule_supported": False,
        "narrow_followup_candidate": (
            "coalesce an immediately adjacent same-user User/User-talk link and "
            "wrapper markup into signature_start"
        ),
        "implementation_recommendation": "broader control audit required; do not implement",
        "conclusion": (
            "Two rows suggest an adjacent same-user split-signature rule, but two contain "
            "substantive continuation; the four cases do not support implementation."
        ),
    }


def run_residual_rule_probe(
    settings: Settings,
    *,
    seed: str = SEED,
    input_path: Path | None = None,
    output_directory: Path | None = None,
) -> dict[str, Any]:
    """Evaluate proofs over an existing evidence parquet without recovery writes."""

    default_input = LLMAuditBundlePaths.from_settings(settings, seed).sample_evidence
    source_path = input_path or default_input
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    rows = pq.read_table(source_path).to_pylist()
    uids = [str(row.get("source_row_uid") or "") for row in rows]
    if not all(uids) or len(set(uids)) != len(uids):
        raise RuntimeError("probe input must contain unique nonempty source_row_uid values")

    frozen_sample = source_path.resolve() == default_input.resolve()
    uid_hash = canonical_json_hash(uids)
    if frozen_sample:
        if len(rows) != FROZEN_SAMPLE_SIZE or uid_hash != FROZEN_SAMPLE_UID_HASH:
            raise RuntimeError("frozen 600 sample identity/order mismatch")
        manifest_path = LLMAuditBundlePaths.from_settings(settings, seed).manifest
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("sample_uid_hash") != FROZEN_SAMPLE_UID_HASH:
            raise RuntimeError("LLM audit bundle manifest sample hash mismatch")

    results = run_probes(rows)
    if len(results) != len(rows) * 5:
        raise RuntimeError("expected exactly five probe results per input row")
    design = {
        str(row["source_row_uid"]): (
            row.get("inclusion_probability"),
            row.get("survey_weight"),
        )
        for row in rows
    }
    for result in results:
        expected = design[str(result["source_row_uid"])]
        if (result.get("inclusion_probability"), result.get("survey_weight")) != expected:
            raise RuntimeError("probe result changed frozen survey-design values")
    summary = summarize_probe_results(rows, results)
    if frozen_sample:
        summary["x1_indent"]["expected_diagnostic_cases"] = 31
        summary["x1_indent"]["colon_compatible_expected_cases"] = 29
        summary["x1_indent"]["intentionally_excluded_non_colon_cases"] = 2
        summary["x1_indent"]["expected_diagnostic_cases_reproduced"] = (
            summary["x1_indent"]["body_identity_matches"] == 31
        )
        summary["x1_indent"]["colon_policy_cases_reproduced"] = (
            summary["x1_indent"]["body_identity_matches"] == 29
        )
    summary.update(
        {
            "diagnostic_only": True,
            "input": file_descriptor(source_path),
            "input_rows": len(rows),
            "sample_uid_hash": uid_hash,
            "frozen_sample_verified": frozen_sample,
            "seed": seed,
        }
    )

    default_paths = RuleProbePaths.from_settings(settings, seed)
    if output_directory is None:
        paths = default_paths
    else:
        paths = RuleProbePaths(
            root=output_directory,
            rows=output_directory / "rule_probe_results.parquet",
            summary=output_directory / "rule_probe_summary.json",
            signature_fragment_diagnostics=(
                output_directory / "x1_signature_fragment_diagnostics.jsonl"
            ),
        )
    table = table_from_union_pylist(results)
    atomic_parquet(paths.rows, table)
    if frozen_sample:
        diagnostics, diagnostic_summary = _build_signature_fragment_diagnostics(rows)
        atomic_write_bytes(
            paths.signature_fragment_diagnostics,
            (
                "".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    for row in diagnostics
                )
            ).encode("utf-8"),
        )
        summary["signature_fragment_investigation"] = {
            **diagnostic_summary,
            "artifact": str(paths.signature_fragment_diagnostics),
        }
    summary["outputs"] = {
        "row_results": {**file_descriptor(paths.rows), "rows": table.num_rows},
        "summary": str(paths.summary),
    }
    atomic_write_json(paths.summary, summary)
    return {
        "rows": str(paths.rows),
        "summary": str(paths.summary),
        "input_rows": len(rows),
        "probe_rows": len(results),
        "rule_families": summary["rule_families"],
        "overlaps": summary["overlaps"],
        "x1_indent": summary["x1_indent"],
        "signature_fragment_investigation": summary.get("signature_fragment_investigation"),
        "frozen_sample_verified": frozen_sample,
    }


def retry_residual_unavailable(
    settings: Settings,
    *,
    seed: str = SEED,
    allow_network: bool = False,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Refresh only the frozen bundle's explicitly retryable revision class."""

    unavailable_path = LLMAuditBundlePaths.from_settings(settings, seed).unavailable
    rows = pq.read_table(unavailable_path).to_pylist()
    taxonomy = Counter(str(row.get("unavailable_taxonomy") or "missing") for row in rows)
    retryable_ids = sorted(
        {
            int(row["target_revision_id"])
            for row in rows
            if row.get("unavailable_taxonomy") == "fetch/cache failure"
            and row.get("target_revision_id") is not None
        }
    )
    plan = {
        "diagnostic_only": not allow_network,
        "input": file_descriptor(unavailable_path),
        "unavailable_rows": len(rows),
        "taxonomy_counts": dict(sorted(taxonomy.items())),
        "retryable_target_revision_ids": len(retryable_ids),
        "network_allowed": allow_network,
    }
    if not allow_network:
        return plan

    method_paths = MethodBPaths.from_settings(settings)
    output_root = RuleProbePaths.from_settings(settings, seed).root / "retryable_acquisition"
    report = hydrate_revision_pairs(
        settings,
        retryable_ids,
        index_path=method_paths.revision_index,
        pairs_path=output_root / "revision_pairs.parquet",
        history_path=output_root / "revision_history.parquet",
        allow_network=True,
        batch_size=batch_size,
    )
    return {**plan, "acquisition": report, "output_directory": str(output_root)}
