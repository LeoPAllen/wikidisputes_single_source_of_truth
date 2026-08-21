# Identity and future annotation join contract

`source_row_uid` is the hash of immutable repository/commit/archive/file/side/
case/row location. Reordering, repaired time, selected text, or future annotations
cannot change it. `source_projection_sha256` is a separate content hash over the
fixed `source-projection-v1` field array; a content mutation changes that hash but
does not change the row key.

Logical IDs prefer `wikiconv:<ancestor-or-creation-id>`. If no authoritative
creation alias is recoverable, `wdutt:fallback:v1:<hash>` uses only an immutable
source alias/location. Derivation method, candidates, confidence and adjudication
state are retained in the append-only identity registry. A later promotion adds an
alias/redirect; it must not rewrite or orphan an issued fallback.

The version 1.0.0 `annotation_join_contract` has one row per source occurrence and
directly retains source/logical/context/action/version/dispute/episode/conversation/
thread identifiers, current/original/ancestor/parent aliases, exact source text and
user, selected-text and source-projection hashes, original file/case/row/order,
canonical/display order, reply target, evidence pointer and all algorithm/schema
versions. Source-linked context rows have `context_node_uid`, no logical utterance
ID, and are not annotation eligible. The versioned
`annotation_context_join_contract` also covers WikiConv-only headings and
talk-page context; the companion display export preserves their position.

The source-row contract path is
`output/canonical/wikidisputes_annotation_join_contract.parquet`, with context at
`output/canonical/wikidisputes_annotation_context_join_contract.parquet`. They are
intended for a later Gold migration, but this repository contains no Gold-reader
or annotation-population step.


## Original-row identity rule

For a WikiDisputes row whose lifecycle type is `original`, the row's current
WikiDisputes ID is the creation identity. `original_id` must not override that
identity or be used to redirect the original observation during WikiConv
matching. For later modification/restoration/deletion observations,
`original_id` links the action back to the logical creation. This rule is
versioned in identity algorithm 1.0.1 and join contract 1.0.1.


## WikiDisputes current-ID rule

`wikidisputes_id_exact` is the stable source-occurrence anchor for substantive
WikiDisputes rows, including rows whose final observed lifecycle type is
`modification` or `restoration`. `wikidisputes_original_id_exact` is retained
as lifecycle/ancestor provenance and as an alias for WikiConv reconciliation,
but it does not replace the current ID as the initial source grouping key.

This prevents the same WikiDisputes current ID from splitting into different
logical utterances when overlapping source cases contain inconsistent
`original_id` metadata. After source grouping, WikiConv current, ancestor, and
lifecycle-action aliases are still used to resolve the authoritative logical
utterance identity.
