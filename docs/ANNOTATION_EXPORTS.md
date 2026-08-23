# Annotation exports

The canonical WikiDisputes SSOT remains immutable. Historical MediaWiki recovery
is a downstream annotation enrichment.

## Workflow

1. `scripts/recover_raw_mediawiki_comments.py` fetches and caches historical page
   revisions, segments signed talk-page comments and matches candidates to known
   WikiDisputes utterances. WikiConv offsets are positional hints rather than
   literal raw-text boundaries.
2. `scripts/promote_raw_mediawiki_comments.py` stores two representations for
   high-confidence matches: the complete archival raw comment and a
   signature-stripped body.
3. `scripts/build_annotation_inputs.py` prefers the high-confidence body for
   annotation. Review, unresolved, no-candidate and unavailable cases retain the
   prior annotation text.
4. Gold migration is non-destructive and preserves legacy `utterance_id`.

Current validated recovery: 106,308 / 133,223 occurrences (79.8%) high-confidence;
105,832 regain markup absent from WikiDisputes.

Large recovery outputs, revision caches and review tables are intentionally not
versioned. Compact validation summaries are retained under `reports/`.
