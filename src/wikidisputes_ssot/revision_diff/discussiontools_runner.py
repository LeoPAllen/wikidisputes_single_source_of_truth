"""Resumable orchestration for the deliberately small DiscussionTools pilot.

This is an evidence collector, not a second boundary detector: the PHP image is
the real DiscussionTools parser and every accepted result is still mapped to a
candidate extracted from immutable archival wikitext.
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import pyarrow.parquet as pq

from wikidisputes_ssot.config import Settings
from wikidisputes_ssot.hashing import canonical_json_hash, sha256_bytes, sha256_file
from wikidisputes_ssot.io import (
    BlobStore,
    atomic_parquet,
    atomic_write_json,
    file_descriptor,
    table_from_union_pylist,
)
from wikidisputes_ssot.promotion_safety import visible_text

from .boundaries import BoundaryCandidate, extract_comment_candidates
from .cache import load_cached_revision_index, resolve_revision_text
from .discussiontools_feasibility import (
    ContaminationStatus,
    FeasibilityResult,
    RenderedComment,
    RenderedMappingEvidence,
    evaluate_rendered_mapping,
    feasibility_report,
    normalise_visible_text,
    select_feasibility_sample,
)
from .discussiontools_state import (
    DiscussionToolsRenderResource,
    DiscussionToolsRevisionState,
    DiscussionToolsStateManifest,
    DiscussionToolsStateStore,
)
from .workflow import MethodBPaths

RUNNER_VERSION = "discussiontools-runner-v2"
RESOURCE_KIND = "historical_parsoid_html"
EXPECTED_HTML_PROFILE = "2.8.0"
EXPECTED_PARSOID_VERSION = "0.24.0.0-alpha21"
EXPECTED_HARNESS_VERSIONS = {
    "php_version": "8.3.27",
    "composer_version": "2.8.12",
    "composer_phar_sha256": "f446ea719708bb85fcbf4ef18def5d0515f1f9b4d703f6d820c9c1656e10a2f2",
    "mediawiki_commit": "dfd080bb34fe9160b027c814e08af29a8e63063c",
    "discussiontools_commit": "16fa124bcf4ad5bb9419abe634d700772bc07be8",
    "visualeditor_commit": "020ff448040df3adac2531d22229b7629a1eb5c3",
    "linter_commit": "df783ad77cba2ae28adb1875ce124b0f67b9758d",
    "parsoid_composer_version": "v0.23.1",
}
DEFAULT_IMAGE = "wikidisputes-discussiontools:rel1_46-pinned"
HARNESS_VERSION_TIMEOUT_SECONDS = 120
HARNESS_BATCH_TIMEOUT_SECONDS = 900
MAX_HTML_BYTES = 32 * 1024 * 1024
DOCKER_SANDBOX_ARGS = (
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
)


@dataclass(frozen=True, slots=True)
class DiscussionToolsPaths:
    sample: Path
    sample_manifest: Path
    state: Path
    html_blobs: Path
    evidence: Path
    report: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> DiscussionToolsPaths:
        silver = settings.roots.output / "silver"
        reports = settings.roots.output / "reports" / "revision_diff"
        root = settings.roots.cache / "discussiontools"
        return cls(
            sample=silver / "discussiontools_feasibility_sample.parquet",
            sample_manifest=reports / "discussiontools_feasibility_sample_manifest.json",
            state=root / "feasibility_state.sqlite",
            html_blobs=root / "historical_html",
            evidence=silver / "discussiontools_feasibility_evidence.parquet",
            report=reports / "discussiontools_feasibility_report.json",
        )


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    return cast(list[dict[str, Any]], pq.read_table(path).to_pylist())


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _revision_id(row: Mapping[str, Any]) -> int | None:
    for name in ("target_revision_id", "revision_id"):
        try:
            value = row.get(name)
            if value not in (None, ""):
                return int(_text(value))
        except (TypeError, ValueError):
            pass
    return None


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _contamination_status(row: Mapping[str, Any]) -> ContaminationStatus:
    value = row.get("contamination_status")
    if isinstance(value, str):
        normalized = value.casefold()
        if normalized == "clean":
            return "clean"
        if normalized == "detected":
            return "detected"
        if normalized == "unknown":
            return "unknown"
    value = row.get("neighboring_comment_contamination")
    if value is True or (isinstance(value, (int, float)) and value == 1):
        return "detected"
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "detected"}:
            return "detected"
        if normalized in {"false", "0", "no", "clean", ""}:
            return "clean"
    if value is False or value == 0:
        return "clean"
    return "unknown"


def _target_spans(row: Mapping[str, Any]) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for value in _json_list(row.get("action_target_changed_ranges_json")):
        if not isinstance(value, list) or len(value) != 2:
            continue
        try:
            start, end = int(_text(value[0])), int(_text(value[1]))
        except ValueError:
            continue
        if start < end:
            spans.append((start, end))
    return tuple(spans)


def _action_count(row: Mapping[str, Any]) -> int:
    """Return an exact positive count, or zero so malformed evidence fails closed."""

    try:
        value = int(_text(row.get("action_count") or 0))
    except ValueError:
        return 0
    return value if value > 0 else 0


def _plausible_candidates(
    candidates: Sequence[BoundaryCandidate], spans: Sequence[tuple[int, int]]
) -> list[BoundaryCandidate]:
    if not spans:
        return []
    return [
        candidate
        for candidate in candidates
        if all(candidate.start <= start < end <= candidate.end for start, end in spans)
    ]


def _control_boundary_agrees(row: Mapping[str, Any], candidate: BoundaryCandidate | None) -> bool:
    if candidate is None:
        return False
    stratum = _text(row.get("discussiontools_stratum"))
    if stratum == "control_method_a_promote":
        start, end, raw = (
            row.get("method_a_left_boundary"),
            row.get("method_a_right_boundary"),
            row.get("method_a_candidate_full_raw"),
        )
    elif stratum == "control_method_b_safe_usable":
        start, end, raw = (
            row.get("candidate_start"),
            row.get("candidate_end"),
            row.get("candidate_raw"),
        )
    else:
        return False
    try:
        return (
            candidate.start == int(_text(start))
            and candidate.end == int(_text(end))
            and candidate.raw_wikitext == raw
        )
    except (TypeError, ValueError):
        return False


def _load_cached_html(blob_root: Path, resource: DiscussionToolsRenderResource) -> bytes:
    relative = Path(str(resource.blob_path))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("cached_html_blob_path_invalid")
    html = gzip.decompress((blob_root / relative).read_bytes())
    if sha256_bytes(html) != resource.content_sha256:
        raise RuntimeError("cached_html_content_hash_mismatch")
    return html


def _single_matched_candidate(
    comments: Sequence[RenderedComment], candidates: Sequence[BoundaryCandidate]
) -> BoundaryCandidate | None:
    pairs = [
        candidate
        for comment in comments
        for candidate in candidates
        if normalise_visible_text(comment.visible_text)
        == normalise_visible_text(visible_text(candidate.body_wikitext))
    ]
    return pairs[0] if len(pairs) == 1 else None


def _missing_revision_result(row: Mapping[str, Any]) -> FeasibilityResult:
    return FeasibilityResult(
        source_row_uid=_text(row.get("source_row_uid")),
        stratum=_text(row.get("discussiontools_stratum")),
        is_control=_text(row.get("discussiontools_stratum")).startswith("control_"),
        parser_success=False,
        contamination_status=_contamination_status(row),
        b_status=_text(row.get("status")),
        lifecycle=_text(row.get("action_type")),
        failure_reasons=("revision_id_missing",),
    )


def _sample_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    return canonical_json_hash(list(rows))


def _path_binding(path: Path) -> dict[str, Any]:
    """Bind an optional cache index without pretending a missing file was read."""

    return file_descriptor(path) if path.exists() else {"path": str(path), "exists": False}


def write_feasibility_sample(settings: Settings, *, seed: str = "20260831") -> dict[str, Any]:
    """Join source/evidence and write the exact deterministic 200-row pilot."""

    from .workflow import MethodBPaths

    paths = DiscussionToolsPaths.from_settings(settings)
    method_paths = MethodBPaths.from_settings(settings)
    source = _rows(method_paths.source_population)
    evidence = _rows(method_paths.recovery_evidence)
    evidence_by_uid: dict[str, dict[str, Any]] = {}
    for row in evidence:
        uid = _text(row.get("source_row_uid"))
        if not uid:
            raise RuntimeError("DiscussionTools recovery evidence has a missing source_row_uid")
        if uid in evidence_by_uid:
            raise RuntimeError(f"duplicate DiscussionTools recovery evidence identity: {uid}")
        evidence_by_uid[uid] = row
    joined: list[dict[str, Any]] = []
    for row in source:
        uid = _text(row.get("source_row_uid"))
        if not uid:
            raise RuntimeError("DiscussionTools source population has a missing source_row_uid")
        joined_row = dict(row)
        # Method-B evidence is authoritative for status/action diagnostics.
        joined_row.update(evidence_by_uid.get(uid, {}))
        joined_row["source_row_uid"] = uid
        for name in ("page_id", "predecessor_revision_id"):
            if joined_row.get(name) is not None:
                joined_row[name] = str(joined_row[name])
        joined.append(joined_row)
    selection = select_feasibility_sample(joined, seed=seed)
    output = [
        {
            **dict(sample.row),
            "discussiontools_stratum": sample.stratum,
            "discussiontools_priority": sample.priority,
            "discussiontools_matching_labels_json": json.dumps(sample.matching_labels),
        }
        for sample in selection.samples
    ]
    sample_hash = _sample_hash(output)
    manifest = {
        "runner_version": RUNNER_VERSION,
        "seed": seed,
        "source_population": file_descriptor(method_paths.source_population),
        "recovery_evidence": file_descriptor(method_paths.recovery_evidence),
        "sample_sha256": sample_hash,
        "sample_count": len(output),
        "requested": dict(selection.requested),
        "selected": dict(selection.selected),
        "shortfalls": dict(selection.shortfalls),
    }
    if not selection.complete:
        raise RuntimeError(f"DiscussionTools sample is incomplete: {selection.shortfalls}")
    atomic_parquet(paths.sample, table_from_union_pylist(output))
    atomic_write_json(paths.sample_manifest, manifest)
    return {"sample": str(paths.sample), "manifest": str(paths.sample_manifest), **manifest}


def _source_url(revision_id: int) -> str:
    return f"https://en.wikipedia.org/w/rest.php/v1/revision/{revision_id}/html"


def _validate_html(html: bytes, revision_id: int, headers: Mapping[str, str]) -> str | None:
    if len(html) > MAX_HTML_BYTES:
        return "html_exceeds_32_mib"
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError:
        return "html_not_utf8"
    content_type = headers.get("content-type", "")
    if EXPECTED_HTML_PROFILE not in content_type:
        return "html_profile_mismatch"
    version = re.search(r"\bdata-mw-parsoid-version=[\"']([^\"']+)", text, re.I)
    if not version or version.group(1) != EXPECTED_PARSOID_VERSION:
        return "parsoid_version_mismatch"
    response_revision = headers.get("content-revision-id")
    if response_revision is not None and response_revision != str(revision_id):
        return "content_revision_id_mismatch"
    return None


def _fetch_html(settings: Settings, revision_id: int) -> tuple[int, dict[str, str], bytes]:
    url = _source_url(revision_id)
    attempts = settings.network.max_attempts
    last: Exception | None = None
    with httpx.Client(
        timeout=settings.network.timeout_seconds,
        headers={"User-Agent": settings.network.user_agent},
    ) as client:
        for attempt in range(attempts):
            try:
                response = client.get(
                    url,
                    headers={
                        "Accept": 'text/html; profile="https://www.mediawiki.org/wiki/Specs/HTML/2.8.0"'
                    },
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay = (
                            float(retry_after)
                            if retry_after is not None
                            else 2**attempt / settings.network.requests_per_second
                        )
                    except ValueError:
                        delay = 2**attempt / settings.network.requests_per_second
                    time.sleep(min(max(delay, 0.0), 30.0))
                    continue
                return (
                    response.status_code,
                    {key.lower(): value for key, value in response.headers.items()},
                    response.content,
                )
            except httpx.HTTPError as error:
                last = error
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 10) / settings.network.requests_per_second)
    raise RuntimeError(f"historical HTML request failed: {last}")


def _harness_versions(image: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "run", "--rm", *DOCKER_SANDBOX_ARGS, image, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=HARNESS_VERSION_TIMEOUT_SECONDS,
    )
    if completed.returncode:
        raise RuntimeError(f"DiscussionTools harness unavailable: {completed.stderr.strip()}")
    lines = completed.stdout.strip().splitlines()
    if len(lines) != 1:
        raise RuntimeError("DiscussionTools harness emitted no unique version response")
    try:
        parsed = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise RuntimeError("DiscussionTools harness emitted invalid version JSON") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("DiscussionTools harness version response must be an object")
    if parsed.get("type") != "discussiontools_harness_versions":
        raise RuntimeError("DiscussionTools harness emitted an invalid version response")
    version_payload = parsed.get("versions")
    if not isinstance(version_payload, Mapping):
        raise RuntimeError("DiscussionTools harness version payload must be an object")
    versions = dict(version_payload)
    expected_versions = dict(EXPECTED_HARNESS_VERSIONS)
    root = Path(__file__).resolve().parents[3]
    local_bindings = {
        "harness_script_sha256": root / "tools/discussiontools/discussiontools_ndjson.php",
        "local_settings_sha256": root / "tools/discussiontools/LocalSettings.php",
        "versions_file_sha256": root / "tools/discussiontools/versions.php",
    }
    expected_versions.update({name: sha256_file(path) for name, path in local_bindings.items()})
    mismatches = {
        name: {"expected": expected, "actual": versions.get(name)}
        for name, expected in expected_versions.items()
        if versions.get(name) != expected
    }
    if mismatches:
        raise RuntimeError(f"DiscussionTools harness version pin mismatch: {mismatches}")
    return versions


def _run_harness(
    image: str,
    inputs: Sequence[Mapping[str, Any]],
    *,
    expected_versions: Mapping[str, Any] | None = None,
) -> dict[int, dict[str, Any]]:
    expected_inputs: dict[int, Mapping[str, Any]] = {}
    for item in inputs:
        revision_id = int(_text(item.get("revision_id")))
        if revision_id in expected_inputs:
            raise ValueError(f"duplicate harness input revision {revision_id}")
        expected_inputs[revision_id] = item
    ndjson = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in inputs)
    completed = subprocess.run(
        ["docker", "run", "--rm", "-i", *DOCKER_SANDBOX_ARGS, image],
        input=ndjson,
        check=False,
        capture_output=True,
        text=True,
        timeout=HARNESS_BATCH_TIMEOUT_SECONDS,
    )
    if completed.returncode:
        raise RuntimeError(f"DiscussionTools harness failed: {completed.stderr.strip()}")
    output: dict[int, dict[str, Any]] = {}
    for line in completed.stdout.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("DiscussionTools harness emitted invalid result JSON") from error
        if not isinstance(parsed, dict):
            raise RuntimeError("DiscussionTools harness result must be an object")
        try:
            revision_id = int(_text(parsed.get("revision_id")))
        except ValueError as error:
            raise RuntimeError("DiscussionTools harness result has no revision ID") from error
        if revision_id not in expected_inputs:
            raise RuntimeError(f"DiscussionTools harness emitted unexpected revision {revision_id}")
        if revision_id in output:
            raise RuntimeError(f"DiscussionTools harness duplicated revision {revision_id}")
        expected_html = expected_inputs[revision_id].get("html")
        result_type = parsed.get("type")
        if result_type not in {"discussiontools_parse", "discussiontools_error"}:
            raise RuntimeError(
                f"DiscussionTools harness emitted an invalid result type for revision {revision_id}"
            )
        if result_type == "discussiontools_parse":
            comments = parsed.get("comments")
            headings = parsed.get("headings")
            if not isinstance(comments, list) or not all(
                isinstance(comment, dict) for comment in comments
            ):
                raise RuntimeError(
                    f"DiscussionTools harness comments are invalid for revision {revision_id}"
                )
            if not isinstance(headings, list) or not all(
                isinstance(heading, dict) for heading in headings
            ):
                raise RuntimeError(
                    f"DiscussionTools harness headings are invalid for revision {revision_id}"
                )
        if isinstance(expected_html, str):
            expected_html_sha256 = sha256_bytes(expected_html.encode("utf-8"))
            if parsed.get("html_sha256") != expected_html_sha256:
                raise RuntimeError(
                    f"DiscussionTools harness HTML hash mismatch for revision {revision_id}"
                )
        if expected_versions is not None and parsed.get("versions") != dict(expected_versions):
            raise RuntimeError(
                f"DiscussionTools harness result version mismatch for revision {revision_id}"
            )
        output[revision_id] = parsed
    missing = sorted(set(expected_inputs).difference(output))
    if missing:
        raise RuntimeError(f"DiscussionTools harness omitted revisions: {missing}")
    return output


def _code_binding() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    files = [
        Path(__file__),
        Path(__file__).with_name("discussiontools_feasibility.py"),
        Path(__file__).with_name("discussiontools_state.py"),
        root / "tools/discussiontools/Dockerfile",
        root / "tools/discussiontools/LocalSettings.php",
        root / "tools/discussiontools/discussiontools_ndjson.php",
        root / "tools/discussiontools/versions.php",
    ]
    return {str(path): sha256_file(path) for path in files}


def _result_from_state(
    row: Mapping[str, Any], state: DiscussionToolsRevisionState
) -> FeasibilityResult:
    payload = state.payload
    mappings = payload.get("mappings")
    if isinstance(mappings, dict):
        candidate = mappings.get(_text(row.get("source_row_uid")))
        if isinstance(candidate, dict):
            payload = candidate
    return FeasibilityResult(
        source_row_uid=_text(row.get("source_row_uid")),
        stratum=_text(row.get("discussiontools_stratum")),
        is_control=_text(row.get("discussiontools_stratum")).startswith("control_"),
        parser_success=state.status == "success" and bool(payload.get("parser_success")),
        exact_boundary_agreement=bool(payload.get("exact_boundary_agreement")),
        contamination_status=_contamination_status(payload),
        proposed_safe=bool(payload.get("proposed_safe")),
        b_status=_text(row.get("status")),
        lifecycle=_text(row.get("action_type")),
        failure_reasons=tuple(payload.get("failure_reasons") or ()),
    )


def run_feasibility(
    settings: Settings,
    *,
    resume: bool = True,
    checkpoint_every: int = 25,
    allow_network: bool = False,
    harness_image: str = DEFAULT_IMAGE,
) -> dict[str, Any]:
    """Fetch/render/parse the saved pilot, committing each checkpoint safely."""
    if not 25 <= checkpoint_every <= 50:
        raise ValueError("checkpoint_every must be between 25 and 50")
    paths = DiscussionToolsPaths.from_settings(settings)
    if not paths.sample.exists() or not paths.sample_manifest.exists():
        raise FileNotFoundError("run discussiontools-sample before discussiontools-feasibility")
    rows = _rows(paths.sample)
    try:
        sample_manifest = json.loads(paths.sample_manifest.read_text())
    except json.JSONDecodeError as error:
        raise RuntimeError("DiscussionTools sample manifest is invalid JSON") from error
    if not isinstance(sample_manifest, dict):
        raise RuntimeError("DiscussionTools sample manifest must be an object")
    if _sample_hash(rows) != sample_manifest.get("sample_sha256"):
        raise RuntimeError("DiscussionTools sample hash mismatch")
    code_binding = _code_binding()
    versions = _harness_versions(harness_image)
    method_paths = MethodBPaths.from_settings(settings)
    manifest = DiscussionToolsStateManifest.from_values(
        config={
            "runner": RUNNER_VERSION,
            "checkpoint_every": checkpoint_every,
            "image": harness_image,
            "expected_html_profile": EXPECTED_HTML_PROFILE,
            "expected_parsoid_version": EXPECTED_PARSOID_VERSION,
            "max_html_bytes": MAX_HTML_BYTES,
            "harness_versions": versions,
            "harness_version_timeout_seconds": HARNESS_VERSION_TIMEOUT_SECONDS,
            "harness_batch_timeout_seconds": HARNESS_BATCH_TIMEOUT_SECONDS,
            "settings": settings.canonical_dict(),
        },
        code=code_binding,
        inputs={
            "sample": file_descriptor(paths.sample),
            "sample_manifest": file_descriptor(paths.sample_manifest),
            "revision_index": _path_binding(method_paths.revision_index),
        },
    )
    records = load_cached_revision_index(
        settings,
        method_paths.revision_index,
        required_ids={value for row in rows if (value := _revision_id(row)) is not None},
    )
    blobs = BlobStore(paths.html_blobs)
    by_revision: dict[int, list[dict[str, Any]]] = {}
    rows_without_revision: list[dict[str, Any]] = []
    for row in rows:
        revision_id = _revision_id(row)
        if revision_id is None:
            rows_without_revision.append(row)
            continue
        by_revision.setdefault(revision_id, []).append(row)
    last_fetch_started: float | None = None
    network_fetch_count = 0
    with DiscussionToolsStateStore(paths.state, manifest, resume=resume) as store:
        if not resume and any(
            store.get(revision_id) is not None
            or store.get_render_resource(revision_id, RESOURCE_KIND) is not None
            for revision_id in by_revision
        ):
            raise RuntimeError(
                "--no-resume refuses an existing state database; use a new cache root or --resume"
            )
        for revision_ids in [
            list(by_revision)[index : index + checkpoint_every]
            for index in range(0, len(by_revision), checkpoint_every)
        ]:
            inputs: list[dict[str, Any]] = []
            raw_by_id: dict[int, str] = {}
            # Hydration is intentionally outside a checkpoint transaction: each
            # fetched immutable resource must survive an interruption before PHP.
            for revision_id in revision_ids:
                if not store.should_process(revision_id):
                    continue
                record = records.get(revision_id)
                revision_text = resolve_revision_text(settings, record) if record else None
                if revision_text is None or revision_text.raw_text is None:
                    store.record(
                        revision_id=revision_id,
                        status="unavailable",
                        raw_wikitext_sha256=None,
                        payload={
                            "parser_success": False,
                            "failure_reasons": ["raw_wikitext_unavailable"],
                        },
                        overwrite=not resume,
                    )
                    continue
                raw_by_id[revision_id] = revision_text.raw_text
                raw_wikitext_sha256 = sha256_bytes(revision_text.raw_text.encode("utf-8"))
                if revision_text.local_content_sha256 not in (None, raw_wikitext_sha256):
                    raise RuntimeError(
                        f"local raw-wikitext hash mismatch for revision {revision_id}"
                    )
                resource = store.get_render_resource(revision_id, RESOURCE_KIND)
                if resource is not None and resource.raw_wikitext_sha256 != raw_wikitext_sha256:
                    raise RuntimeError(
                        f"render resource raw-wikitext hash mismatch for revision {revision_id}"
                    )
                if resource is None:
                    if not allow_network:
                        # Deliberately no resource record: a later opt-in may fetch it.
                        continue
                    try:
                        if last_fetch_started is not None:
                            interval = 1.0 / settings.network.requests_per_second
                            time.sleep(max(0.0, interval - (time.monotonic() - last_fetch_started)))
                        last_fetch_started = time.monotonic()
                        network_fetch_count += 1
                        status, headers, content = _fetch_html(settings, revision_id)
                    except Exception as error:
                        store.record_render_resource(
                            revision_id=revision_id,
                            resource_kind=RESOURCE_KIND,
                            status="failed",
                            raw_wikitext_sha256=raw_wikitext_sha256,
                            source_url=_source_url(revision_id),
                            error={"reason": "http_error", "detail": str(error)},
                            overwrite=not resume,
                        )
                        resource = store.get_render_resource(revision_id, RESOURCE_KIND)
                    else:
                        descriptor = blobs.put_gzip(content, suffix=".html.gz")
                        http_metadata = {
                            "status_code": status,
                            "content_type": headers.get("content-type", ""),
                            "content_revision_id": headers.get("content-revision-id", ""),
                            "etag": headers.get("etag", ""),
                            "last_modified": headers.get("last-modified", ""),
                        }
                        validation = _validate_html(content, revision_id, headers)
                        if status != 200 or validation:
                            store.record_render_resource(
                                revision_id=revision_id,
                                resource_kind=RESOURCE_KIND,
                                status="failed",
                                raw_wikitext_sha256=raw_wikitext_sha256,
                                source_url=_source_url(revision_id),
                                content_sha256=descriptor["content_sha256"],
                                blob_path=descriptor["blob_path"],
                                http_metadata=http_metadata,
                                error={"reason": validation or f"http_status_{status}"},
                                overwrite=not resume,
                            )
                        else:
                            store.record_render_resource(
                                revision_id=revision_id,
                                resource_kind=RESOURCE_KIND,
                                status="success",
                                raw_wikitext_sha256=raw_wikitext_sha256,
                                source_url=_source_url(revision_id),
                                content_sha256=descriptor["content_sha256"],
                                blob_path=descriptor["blob_path"],
                                http_metadata=http_metadata,
                                overwrite=not resume,
                            )
                        resource = store.get_render_resource(revision_id, RESOURCE_KIND)
                if resource is None:
                    # A policy-only cache miss is intentionally not terminal state:
                    # ``--allow-network`` on a later resume must still hydrate it.
                    continue
                if resource.status != "success":
                    store.record(
                        revision_id=revision_id,
                        status=resource.status,
                        raw_wikitext_sha256=raw_wikitext_sha256,
                        payload={
                            "parser_success": False,
                            "failure_reasons": [
                                str((resource.error or {}).get("reason", "render_unavailable"))
                            ],
                        },
                        overwrite=not resume,
                    )
                    continue
                try:
                    html_bytes = _load_cached_html(paths.html_blobs, resource)
                    html_text = html_bytes.decode("utf-8")
                except Exception as error:
                    store.record(
                        revision_id=revision_id,
                        status="failed",
                        raw_wikitext_sha256=raw_wikitext_sha256,
                        payload={
                            "parser_success": False,
                            "failure_reasons": ["cached_html_invalid"],
                        },
                        error={"reason": str(error)},
                        overwrite=not resume,
                    )
                    continue
                title = _text(record.title if record else None) or "Talk:Unknown"
                inputs.append({"revision_id": revision_id, "title": title, "html": html_text})
            if inputs:
                # A process-wide harness failure is operational, not per-revision
                # evidence. Abort without terminal parser rows so resume can retry
                # from the already durable HTML resources.
                parsed = _run_harness(harness_image, inputs, expected_versions=versions)
                with store.transaction():
                    for item in inputs:
                        revision_id = int(item["revision_id"])
                        parsed_row = parsed[revision_id]
                        raw_wikitext = raw_by_id[revision_id]
                        if parsed_row.get("type") != "discussiontools_parse":
                            store.record(
                                revision_id=revision_id,
                                status="failed",
                                raw_wikitext_sha256=sha256_bytes(raw_wikitext.encode()),
                                payload={
                                    "parser_success": False,
                                    "failure_reasons": ["discussiontools_parse_error"],
                                    "parser": parsed_row,
                                },
                                error={
                                    "reason": str(parsed_row.get("error", "invalid_harness_output"))
                                },
                                overwrite=not resume,
                            )
                            continue
                        comments = tuple(
                            RenderedComment(
                                visible_text=_text(comment.get("body_text", comment.get("text"))),
                                author=comment.get("author"),
                                timestamp=comment.get("timestamp_text"),
                                dom_anchor=json.dumps(comment.get("range"), sort_keys=True),
                            )
                            for comment in parsed_row.get("comments", [])
                        )
                        source_rows = by_revision[revision_id]
                        candidates = extract_comment_candidates(raw_wikitext)
                        mappings: dict[str, dict[str, Any]] = {}
                        for evidence_row in source_rows:
                            spans = _target_spans(evidence_row)
                            plausible = _plausible_candidates(candidates, spans)
                            plausible_bodies = {
                                normalise_visible_text(visible_text(candidate.body_wikitext))
                                for candidate in plausible
                            }
                            matching_comments = tuple(
                                comment
                                for comment in comments
                                if comment.visible_text
                                and any(
                                    normalise_visible_text(comment.visible_text) == body
                                    for body in plausible_bodies
                                )
                            )
                            decision = evaluate_rendered_mapping(
                                matching_comments,
                                plausible,
                                RenderedMappingEvidence(
                                    target_spans=spans,
                                    action_count=_action_count(evidence_row),
                                    competing_candidate_count=len(
                                        _json_list(evidence_row.get("competing_candidates_json"))
                                    ),
                                    competing_action_count=max(0, len(source_rows) - 1),
                                    lifecycle_consistent=_text(
                                        evidence_row.get("lifecycle_consistency")
                                    )
                                    == "target_change_localized",
                                    contamination_status=_contamination_status(evidence_row),
                                ),
                            )
                            matched_candidate = _single_matched_candidate(
                                matching_comments, plausible
                            )
                            mappings[_text(evidence_row.get("source_row_uid"))] = {
                                "parser_success": True,
                                "exact_boundary_agreement": _control_boundary_agrees(
                                    evidence_row, matched_candidate
                                ),
                                "contamination_status": _contamination_status(evidence_row),
                                "proposed_safe": decision.safe,
                                "failure_reasons": list(decision.failure_reasons),
                                "candidate_uid": decision.candidate.candidate_uid
                                if decision.candidate
                                else None,
                                "provenance_tag": decision.provenance_tag,
                            }
                        payload = {
                            "parser_success": True,
                            "parser": parsed_row,
                            "mappings": mappings,
                        }
                        store.record(
                            revision_id=revision_id,
                            status="success",
                            raw_wikitext_sha256=sha256_bytes(raw_wikitext.encode()),
                            content_hashes={"html": sha256_bytes(item["html"].encode())},
                            payload=payload,
                            overwrite=not resume,
                        )
        evidence_rows: list[dict[str, Any]] = []
        results: list[FeasibilityResult] = []
        revision_status_counts: Counter[str] = Counter()
        render_status_counts: Counter[str] = Counter()
        for revision_id, source_rows in by_revision.items():
            state = store.get(revision_id)
            resource = store.get_render_resource(revision_id, RESOURCE_KIND)
            revision_status_counts[state.status if state else "pending_network"] += 1
            render_status_counts[resource.status if resource else "missing"] += 1
            for row in source_rows:
                result = (
                    _result_from_state(row, state)
                    if state is not None
                    else FeasibilityResult(
                        source_row_uid=_text(row.get("source_row_uid")),
                        stratum=_text(row.get("discussiontools_stratum")),
                        is_control=_text(row.get("discussiontools_stratum")).startswith("control_"),
                        parser_success=False,
                        b_status=_text(row.get("status")),
                        lifecycle=_text(row.get("action_type")),
                        failure_reasons=("network_disallowed_cache_miss",),
                    )
                )
                results.append(result)
                evidence_rows.append(
                    {
                        **row,
                        "discussiontools_revision_id": revision_id,
                        "discussiontools_state_status": state.status
                        if state
                        else "pending_network",
                        "discussiontools_payload_json": json.dumps(state.payload, sort_keys=True)
                        if state
                        else None,
                        "discussiontools_error_json": json.dumps(state.error, sort_keys=True)
                        if state and state.error
                        else None,
                        **asdict(result),
                    }
                )
        for row in rows_without_revision:
            result = _missing_revision_result(row)
            results.append(result)
            evidence_rows.append(
                {
                    **row,
                    "discussiontools_revision_id": None,
                    "discussiontools_state_status": "unavailable",
                    "discussiontools_payload_json": None,
                    "discussiontools_error_json": json.dumps({"reason": "revision_id_missing"}),
                    **asdict(result),
                }
            )
        operational = {
            "sample_row_count": len(rows),
            "revision_count": len(by_revision),
            "rows_without_revision": len(rows_without_revision),
            "revision_status_counts": dict(sorted(revision_status_counts.items())),
            "render_status_counts": dict(sorted(render_status_counts.items())),
            "allow_network": allow_network,
            "network_fetch_count": network_fetch_count,
            "resume": resume,
            "checkpoint_every": checkpoint_every,
        }
    report_obj = feasibility_report(results)
    report = {
        "manifest": manifest.to_dict(),
        "harness_versions": versions,
        "operational": operational,
        "report": {
            "overall": dict(report_obj.overall),
            "controls": dict(report_obj.controls),
            "residual": dict(report_obj.residual),
            "parser_subgroups": {
                key: dict(value) for key, value in report_obj.parser_subgroups.items()
            },
            "gate": asdict(report_obj.gate),
        },
    }
    atomic_parquet(paths.evidence, table_from_union_pylist(evidence_rows))
    report["artifacts"] = {
        "sample": file_descriptor(paths.sample),
        "sample_manifest": file_descriptor(paths.sample_manifest),
        "state": file_descriptor(paths.state),
        "html_blobs": str(paths.html_blobs),
        "evidence": file_descriptor(paths.evidence),
    }
    atomic_write_json(paths.report, report)
    return {"evidence": str(paths.evidence), "report": str(paths.report), **report["report"]}


__all__ = ["DEFAULT_IMAGE", "DiscussionToolsPaths", "run_feasibility", "write_feasibility_sample"]
