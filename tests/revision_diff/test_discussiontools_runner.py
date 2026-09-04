import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from wikidisputes_ssot.hashing import sha256_bytes, sha256_file
from wikidisputes_ssot.io import atomic_parquet, atomic_write_json, table_from_union_pylist
from wikidisputes_ssot.revision_diff.boundaries import BoundaryCandidate
from wikidisputes_ssot.revision_diff.discussiontools_runner import (
    DOCKER_SANDBOX_ARGS,
    EXPECTED_HARNESS_VERSIONS,
    EXPECTED_PARSOID_VERSION,
    DiscussionToolsPaths,
    _contamination_status,
    _control_boundary_agrees,
    _fetch_html,
    _harness_versions,
    _load_cached_html,
    _missing_revision_result,
    _plausible_candidates,
    _run_harness,
    _sample_hash,
    _validate_html,
    run_feasibility,
)


def _candidate() -> BoundaryCandidate:
    return BoundaryCandidate(
        candidate_uid="candidate",
        start=10,
        end=30,
        raw_wikitext="comment -- sig",
        body_start=10,
        body_end=20,
        body_wikitext="comment",
        signature_start=21,
        signature_end=30,
        raw_signature_wikitext="-- sig",
        signature_timestamp="time",
        signature_user_target="User",
        indentation="",
        depth=0,
        boundary_evidence=("signature",),
        boundary_warnings=(),
    )


def test_contamination_status_does_not_treat_false_string_as_detected() -> None:
    assert _contamination_status({"neighboring_comment_contamination": "false"}) == "clean"
    assert _contamination_status({"neighboring_comment_contamination": "true"}) == "detected"
    assert _contamination_status({}) == "unknown"


def test_action_candidate_filter_and_control_equality_are_exact() -> None:
    candidate = _candidate()
    assert _plausible_candidates([candidate], ((11, 15),)) == [candidate]
    assert _plausible_candidates([candidate], ((9, 15),)) == []
    control = {
        "discussiontools_stratum": "control_method_b_safe_usable",
        "candidate_start": 10,
        "candidate_end": 30,
        "candidate_raw": "comment -- sig",
    }
    assert _control_boundary_agrees(control, candidate)
    assert not _control_boundary_agrees({**control, "candidate_end": 29}, candidate)


def test_cached_gzip_hash_mismatch_is_rejected(tmp_path) -> None:
    blob = tmp_path / "sha256" / "xx" / "bad.gz"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(gzip.compress(b"historical html", mtime=0))
    with pytest.raises(RuntimeError, match="content_hash_mismatch"):
        _load_cached_html(
            tmp_path, SimpleNamespace(blob_path="sha256/xx/bad.gz", content_sha256="0" * 64)
        )
    with pytest.raises(RuntimeError, match="blob_path_invalid"):
        _load_cached_html(
            tmp_path,
            SimpleNamespace(blob_path="../outside.gz", content_sha256="0" * 64),
        )


def test_missing_revision_row_is_explicit_unavailable_evidence() -> None:
    result = _missing_revision_result(
        {"source_row_uid": "missing", "discussiontools_stratum": "b_no_candidate"}
    )
    assert not result.parser_success
    assert result.failure_reasons == ("revision_id_missing",)


def test_fetch_html_uses_exact_historical_revision_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs: object) -> None:
            captured["init"] = kwargs

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, **kwargs: object) -> SimpleNamespace:
            captured["url"] = url
            captured["request"] = kwargs
            return SimpleNamespace(
                status_code=200,
                headers={"Content-Type": "text/html; profile=2.8.0"},
                content=b"html",
            )

    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.httpx.Client", Client
    )
    settings = SimpleNamespace(
        network=SimpleNamespace(
            max_attempts=1,
            timeout_seconds=5,
            user_agent="WikiDisputesSSOT test",
            requests_per_second=1,
        )
    )
    assert _fetch_html(settings, 123) == (
        200,
        {"content-type": "text/html; profile=2.8.0"},
        b"html",
    )
    assert captured["url"] == "https://en.wikipedia.org/w/rest.php/v1/revision/123/html"


def test_fetch_html_retries_transient_status_without_losing_final_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            SimpleNamespace(
                status_code=503,
                headers={"Retry-After": "0"},
                content=b"temporary",
            ),
            SimpleNamespace(
                status_code=200,
                headers={"Content-Type": "text/html; profile=2.8.0"},
                content=b"historical html",
            ),
        ]
    )

    class Client:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return next(responses)

    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.httpx.Client", Client
    )
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.time.sleep",
        lambda delay: None,
    )
    settings = SimpleNamespace(
        network=SimpleNamespace(
            max_attempts=2,
            timeout_seconds=5,
            user_agent="WikiDisputesSSOT test",
            requests_per_second=1,
        )
    )
    assert _fetch_html(settings, 123) == (
        200,
        {"content-type": "text/html; profile=2.8.0"},
        b"historical html",
    )


def test_validate_html_rejects_version_mismatch() -> None:
    html = (
        b'<html><body><div class="mw-parser-output" data-mw-parsoid-version="wrong">'
        b'<meta name="mw:revisionId" content="123" /></body></html>'
    )
    assert (
        _validate_html(html, 123, {"content-type": "profile=2.8.0"}) == "parsoid_version_mismatch"
    )


def test_validate_html_accepts_version_marker_on_parser_output_element() -> None:
    html = (
        b'<html><head><meta property="mw:htmlVersion" content="2.8.0"></head>'
        b'<body><div class="mw-parser-output" data-mw-parsoid-version="'
        + EXPECTED_PARSOID_VERSION.encode()
        + b'"><meta name="mw:revisionId" content="123"></div></body></html>'
    )
    assert _validate_html(html, 123, {"content-type": "profile=2.8.0"}) is None
    assert _validate_html(b"\xff", 123, {"content-type": "profile=2.8.0"}) == "html_not_utf8"


def test_harness_versions_rejects_failed_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="image absent"),
    )
    with pytest.raises(RuntimeError, match="harness unavailable"):
        _harness_versions("missing")


def test_harness_versions_rejects_pin_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {"type": "discussiontools_harness_versions", "versions": EXPECTED_HARNESS_VERSIONS}
    response["versions"] = {**EXPECTED_HARNESS_VERSIONS, "linter_commit": "wrong"}
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(response) + "\n", stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="version pin mismatch"):
        _harness_versions("image")


def test_harness_versions_accepts_exact_image_and_local_code_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    versions = {
        **EXPECTED_HARNESS_VERSIONS,
        "harness_script_sha256": sha256_file(
            root / "tools/discussiontools/discussiontools_ndjson.php"
        ),
        "local_settings_sha256": sha256_file(root / "tools/discussiontools/LocalSettings.php"),
        "versions_file_sha256": sha256_file(root / "tools/discussiontools/versions.php"),
    }
    response = {"type": "discussiontools_harness_versions", "versions": versions}
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(response) + "\n", stderr=""
        ),
    )
    assert _harness_versions("image") == versions


def test_harness_batch_parses_ndjson_and_detects_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = {
        "type": "discussiontools_parse",
        "revision_id": 7,
        "comments": [],
        "headings": [],
        "versions": {"parsoid": EXPECTED_PARSOID_VERSION},
    }
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(parsed) + "\n", stderr=""
        ),
    )
    assert _run_harness("image", [{"revision_id": 7}])[7] == parsed

    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(parsed) + "\n" + json.dumps(parsed) + "\n",
            stderr="",
        ),
    )
    with pytest.raises(RuntimeError, match="duplicated revision"):
        _run_harness("image", [{"revision_id": 7}])


def test_harness_batch_rejects_missing_or_invalid_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    with pytest.raises(RuntimeError, match="omitted revisions"):
        _run_harness("image", [{"revision_id": 7}])

    row = {"type": "unexpected", "revision_id": 7}
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(row) + "\n", stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="invalid result type"):
        _run_harness("image", [{"revision_id": 7}])


def test_harness_process_is_networkless_read_only_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        row = {"type": "discussiontools_error", "revision_id": 7, "error": "fixture"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(row) + "\n", stderr="")

    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.subprocess.run", run
    )
    _run_harness("image", [{"revision_id": 7}])
    command = captured["command"]
    assert isinstance(command, list)
    assert all(argument in command for argument in DOCKER_SANDBOX_ARGS)
    assert captured["kwargs"]["timeout"] == 900  # type: ignore[index]


def test_harness_output_is_bound_to_requested_revision_html_and_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = "<p>exact historical render</p>"
    versions = {"discussiontools_commit": "pinned"}
    parsed = {
        "type": "discussiontools_parse",
        "revision_id": 7,
        "html_sha256": sha256_bytes(html.encode()),
        "versions": versions,
        "comments": [],
        "headings": [],
    }

    def response(row: dict[str, object]) -> None:
        monkeypatch.setattr(
            "wikidisputes_ssot.revision_diff.discussiontools_runner.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout=json.dumps(row) + "\n", stderr=""
            ),
        )

    response(parsed)
    assert (
        _run_harness("image", [{"revision_id": 7, "html": html}], expected_versions=versions)[7]
        == parsed
    )

    response({**parsed, "html_sha256": "0" * 64})
    with pytest.raises(RuntimeError, match="HTML hash mismatch"):
        _run_harness("image", [{"revision_id": 7, "html": html}])

    response({**parsed, "revision_id": 8})
    with pytest.raises(RuntimeError, match="unexpected revision"):
        _run_harness("image", [{"revision_id": 7, "html": html}])

    response({**parsed, "versions": {"discussiontools_commit": "different"}})
    with pytest.raises(RuntimeError, match="result version mismatch"):
        _run_harness("image", [{"revision_id": 7, "html": html}], expected_versions=versions)


def test_feasibility_run_is_cache_only_then_hydrates_and_resumes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(
        roots=SimpleNamespace(
            data=tmp_path / "data",
            cache=tmp_path / "cache",
            output=tmp_path / "output",
        ),
        network=SimpleNamespace(requests_per_second=1),
        canonical_dict=lambda: {"test_root": str(tmp_path)},
    )
    paths = DiscussionToolsPaths.from_settings(settings)
    row = {
        "source_row_uid": "source-1",
        "target_revision_id": 7,
        "discussiontools_stratum": "b_review",
        "status": "b_review",
        "action_type": "creation",
        "action_count": 1,
        "action_target_changed_ranges_json": "[[11, 15]]",
        "competing_candidates_json": "[]",
        "lifecycle_consistency": "target_change_localized",
        "neighboring_comment_contamination": "clean",
    }
    atomic_parquet(paths.sample, table_from_union_pylist([row]))
    atomic_write_json(paths.sample_manifest, {"sample_sha256": _sample_hash([row])})

    raw = "0123456789comment -- sig0123456789"
    record = SimpleNamespace(title="Talk:Fixture")
    revision_text = SimpleNamespace(
        raw_text=raw,
        local_content_sha256=sha256_bytes(raw.encode()),
    )
    calls = {"fetch": 0, "parse": 0}

    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner._harness_versions",
        lambda image: {"discussiontools_commit": "pinned"},
    )
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.load_cached_revision_index",
        lambda *args, **kwargs: {7: record},
    )
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.resolve_revision_text",
        lambda *args, **kwargs: revision_text,
    )
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner.extract_comment_candidates",
        lambda value: [_candidate()],
    )
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner._validate_html",
        lambda *args, **kwargs: None,
    )

    def fetch(*args: object, **kwargs: object) -> tuple[int, dict[str, str], bytes]:
        calls["fetch"] += 1
        return 200, {"content-type": "profile=2.8.0"}, b"<html>fixture</html>"

    def parse(*args: object, **kwargs: object) -> dict[int, dict[str, object]]:
        calls["parse"] += 1
        return {
            7: {
                "type": "discussiontools_parse",
                "revision_id": 7,
                "comments": [
                    {
                        "text": "comment",
                        "author": "User",
                        "timestamp_text": "time",
                        "range": {},
                    }
                ],
            }
        }

    monkeypatch.setattr("wikidisputes_ssot.revision_diff.discussiontools_runner._fetch_html", fetch)
    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner._run_harness", parse
    )

    cache_only = run_feasibility(settings, allow_network=False)
    assert calls == {"fetch": 0, "parse": 0}
    assert cache_only["overall"]["parser_success_count"] == 0
    cache_only_report = json.loads(paths.report.read_text())
    assert cache_only_report["operational"]["revision_status_counts"] == {"pending_network": 1}
    assert cache_only_report["artifacts"]["state"]["sha256"]

    def fail_parse(*args: object, **kwargs: object) -> dict[int, dict[str, object]]:
        calls["parse"] += 1
        raise RuntimeError("container stopped")

    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner._run_harness", fail_parse
    )
    with pytest.raises(RuntimeError, match="container stopped"):
        run_feasibility(settings, allow_network=True)
    assert calls == {"fetch": 1, "parse": 1}

    monkeypatch.setattr(
        "wikidisputes_ssot.revision_diff.discussiontools_runner._run_harness", parse
    )
    hydrated = run_feasibility(settings, allow_network=True)
    assert calls == {"fetch": 1, "parse": 2}
    assert hydrated["overall"]["parser_success_count"] == 1
    assert pq.read_table(paths.evidence).to_pylist()[0]["proposed_safe"] is True
    hydrated_report = json.loads(paths.report.read_text())
    assert hydrated_report["operational"]["revision_status_counts"] == {"success": 1}
    assert hydrated_report["operational"]["render_status_counts"] == {"success": 1}

    resumed = run_feasibility(settings, allow_network=True)
    assert calls == {"fetch": 1, "parse": 2}
    assert resumed["overall"]["parser_success_count"] == 1
