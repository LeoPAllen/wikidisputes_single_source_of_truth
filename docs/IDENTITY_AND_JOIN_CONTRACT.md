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


## Authoritative logical identity

Each source occurrence keeps its own immutable `source_row_uid`, current ID,
`original_id`, action ID, and provenance. Those occurrence-level anchors do not
by themselves define a logical turn. Logical identity comes from authoritative
creation/root lifecycle evidence: WikiConv ancestor/original relationships,
WikiDisputes `original_id`, and exact action-ID aliases. Modifications and
restorations remain versions of the root comment; their actor and action time
cannot replace the creator or creation time.

When an equivalent occurrence supplies a uniquely authoritative root, propagate
that root through exact aliases to occurrences missing it. Reconcile conflicting
root candidates using stronger lifecycle evidence and retain the evidence and
resolution in diagnostics. If the conflict cannot be resolved, fail closed and
report it; do not choose a root by current-ID preference, text similarity, or
signature. Preserve all source occurrences and provenance even when they map to
one logical utterance. This contract supersedes the 1.0.1 current-ID grouping
rules and must be reflected in the active identity algorithm version.
