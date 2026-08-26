# Annotation exports

The canonical WikiDisputes SSOT remains immutable. Historical MediaWiki recovery
is a downstream annotation enrichment.

## Workflow

1. `scripts/recover_raw_mediawiki_comments.py` fetches and caches historical page
   revisions, segments signed talk-page comments and matches candidates to known
   WikiDisputes utterances. WikiConv offsets are positional hints rather than
   literal raw-text boundaries.
2. `scripts/promote_raw_mediawiki_comments.py` treats V3.3 as a candidate
   generator, then compares each candidate with trusted text for the same stable
   source occurrence. The gate combines ordered token retention, critical-token
   preservation, unmatched prose spans, structural/signature checks, adjacent
   utterance checks, and V3.3 evidence. `high_confidence` is necessary but not
   sufficient.
3. Only safety-passed bodies receive the annotation-preferred
   `mediawiki_revision_comment_wikitext_body` kind. Rejected bodies use the
   audit-only `mediawiki_revision_comment_wikitext_body_candidate` kind; raw
   comments and diagnostics remain available for review.
4. `scripts/build_annotation_inputs.py` prefers only the safety-passed body.
   Review, unresolved, rejected and unavailable cases retain trusted fallback
   text.
5. Gold migration is non-destructive and preserves legacy `utterance_id`.

Recovery and promotion now fail closed if `ssot_source_text_exact` differs from
`wikidisputes_annotation_join_contract.parquet` for the same
`ssot_source_row_uid`. Downstream annotation/display text is never accepted as a
replacement recovery target. Positively diagnosed terminal unsigned-attribution
and conventional valediction artifacts may be ignored for comparison only; the
trusted fallback remains immutable, and the same strings on the candidate side
remain contamination.

The canonical Gold calibration has 438 rows (404 substantive and 34 context):
310 candidates pass automatic promotion, 79 retain trusted fallback because V3.3
is not high-confidence, and 15 high-confidence candidates remain for review.
All coder/evidence fields in the current Gold input shell are blank, so no coded
span currently requires re-anchoring; `utterance_text_legacy` remains preserved.

Recovery-candidate counts and safe-promotion counts are reported separately.
Never infer annotation promotion coverage from V3.3 `high_confidence` counts.

Method B is a separate revision-diff channel. Method-A-safe bodies remain the
first selection and byte-identical; only A fallback/review plus `b_safe` may
select B. Merely installing or running Method B does not rebuild annotation
exports. See `REVISION_DIFF_RECOVERY.md` for the explicit validation-gated rebuild.

Large recovery outputs, revision caches and review tables are intentionally not
versioned. Compact validation summaries are retained under `reports/`.
