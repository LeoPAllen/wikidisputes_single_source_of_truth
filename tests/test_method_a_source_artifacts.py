import importlib.util
from pathlib import Path


def _recovery_module():
    path = Path("scripts/recover_raw_mediawiki_comments.py")
    spec = importlib.util.spec_from_file_location("method_a_recovery", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(text: str, index: int = 0) -> dict:
    return {
        "raw": text,
        "body_without_signature": text,
        "start": index,
        "end": index + len(text),
        "anchor_index": index,
    }


def test_exact_source_is_immutable_and_wins_before_artifact_fallback() -> None:
    recovery = _recovery_module()
    source = "Complete statement. – ·"
    source_before = source
    ranked = recovery.rank_candidates(
        source,
        [_candidate(source), _candidate("Complete statement.", 100)],
        0,
    )

    assert source == source_before
    assert ranked[0]["body_without_signature"] == source
    assert ranked[0]["source_comparison_mode"] == "exact"
    assert ranked[0]["source_signature_artifact_stripped"] is False
    assert ranked[0]["source_signature_artifact_reason"] == "terminal_wikidisputes_signature_glyphs"


def test_certified_artifact_fallback_runs_only_after_exact_fails() -> None:
    recovery = _recovery_module()
    source = "Complete statement. – ·"
    ranked = recovery.rank_candidates(source, [_candidate("Complete statement.")], 0)

    assert ranked[0]["source_comparison_mode"] == "certified_source_artifact"
    assert ranked[0]["source_signature_artifact_stripped"] is True
    assert ranked[0]["source_signature_artifact_reason"] == "terminal_wikidisputes_signature_glyphs"
    status, _ = recovery.classify(ranked[0], None)
    assert status == "high_confidence"
