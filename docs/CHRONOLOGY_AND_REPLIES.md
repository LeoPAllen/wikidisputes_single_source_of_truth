# Chronology, lifecycle, and reply repair

Logical chronology is time-first. For each logical utterance, `created_at_utc`
uses the strongest available creation-time evidence, in this order: MediaWiki's
timestamp for the identified creation revision; corrected WikiConv creation time;
then WikiDisputes source time normalized from Europe/London wall time when tied to
an authoritative creation identity. No modification, deletion, or restoration
action time may stand in for creation time. Modified comments retain the root's
creator and creation time.

Europe/London normalization handles winter and BST offsets with `zoneinfo`. When
a wall time falls in an ambiguous DST fold, prefer independently stronger
creation evidence; without it, leave the time unresolved and flag the ambiguity.
Do not guess a fold or manufacture a date. The raw timestamp and normalized
evidence source remain available for validation.

Only rows with validated creation timestamps are chronology-eligible. Those rows
receive a nullable `chronology_rank` ordered by normalized creation UTC; exact ties
use numeric creation revision/position and stable identity keys while retaining a
simultaneity group. A row with unresolved creation time has no chronology rank.
`display_utterance_order` and `display_order` are deterministic presentation
sequences and must not be read as exact chronology. For presentation only,
resolved reply edges constrain parents before children. A known parent's creation
time supplies a lower bound for an unresolved reply, and a known reply's creation
time supplies an upper bound for its unresolved parent. Remaining ambiguous rows
use fewer direct replies first, then stable source and identity keys. This inference
never creates a timestamp, chronology rank, or reply edge. The derivative
`canonical/wikidisputes_chronology_strict.parquet` contains eligible rows only;
unresolved rows remain in canonical, provenance, and annotation outputs.
Diagnostics expose selected evidence, failed-tier fallthrough, uncertainty, and
unresolved timestamps. Validation checks monotonic creation UTC by rank.

Lifecycle actions remain distinct versions of one logical utterance. Source and
WikiConv observations can both evidence that lifecycle. Unresolved source-only
actions remain explicit rather than being fabricated as known creations. Each action
retains `raw_timestamp` and separately records normalized `event_time_utc`, status,
source, timezone, and semantics. Temporal comparisons use normalized values with
compatible semantics; raw timestamps remain evidence only.

Creation, immediately-pre-first-reply and episode-cutoff representation pointers
are selected from lifecycle evidence at or before the relevant time. A
modification/deletion/restoration sharing the exact cutoff timestamp is excluded
from predictor selection and flagged as equal-time uncertainty; the deterministic
action tie-break is not treated as causal knowledge. Episode-keyed predictor-safe
views require an observed representation at or before the episode index. Outcome
eligibility is enabled only by the versioned
`probable-page-title-selected-conversation-v1` rule when the selected conversation is
observed, the stable page ID and title match, the episode has an index time, and the
episode is not quarantined. This produces `eligible_probable_alignment_v1`, not a claim
of exact or manually verified alignment; unresolved and cross-label-quarantined episodes
remain ineligible.

Reply repair resolves raw targets through current, original, ancestor and parent
aliases. Every edge retains its raw target, selected logical target, method/status,
reason, confidence, child-before-parent/equal-time flags, lag and indentation.
Ambiguous/unresolved targets are not discarded. Self-reference and lifecycle/reply
cycles are quality flags or quarantined branches. A target whose known creation time
postdates its known child fails closed: the candidate and both timestamps remain in
reply evidence, while the logical target is cleared and the edge is marked unresolved
instead of rewriting creation time. Validation rejects any resolved known child that
predates a known parent and separately reports known-child/unknown-parent cases without
assigning an assumed parent time.

Human display order is separate from chronology: headings retain descriptive
`row_kind=context` provenance and a context-node identity, without being promoted
to logical utterances. Every source occurrence, including context-classified rows,
remains annotation-eligible in the source-row contract. Article edits only occur in
the event timeline.


## Validated MediaWiki creation timestamps

Creation timestamp validation compares each normalized value with its actual
evidence source (MediaWiki UTC, corrected WikiConv creation time, or normalized
WikiDisputes source time). The validator also detects action-time-as-creation,
missing creation evidence, alias splits, unresolved root conflicts, and stale
report/code metadata. Rows without creation-time evidence remain explicit; no
revision number or action timestamp is converted into a date.

Retained MediaWiki evidence is merged from the timestamp snapshot and batched
talk-page revision observations. Exact duplicate revision timestamps are accepted;
conflicting timestamps for the same revision fail closed instead of selecting one.
