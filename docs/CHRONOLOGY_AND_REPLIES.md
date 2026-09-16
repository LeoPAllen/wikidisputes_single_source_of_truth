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

Known `created_at_utc` is the primary canonical key. Exact ties use numeric
creation revision/position, stable source order, then logical UID; these tie-breaks
do not imply causal order. Unknown-time rows remain explicit and use deterministic
fallback placement that cannot introduce an inversion among known-time rows.
Ordering diagnostics expose the selected evidence, uncertainty, and unresolved
timestamps. Validation enumerates every known-time inversion.

Lifecycle actions remain distinct versions of one logical utterance. Source and
WikiConv observations can both evidence that lifecycle. Unresolved source-only
actions remain explicit rather than being fabricated as known creations.

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
cycles are quality flags or quarantined branches.

Human display order is separate from utterance order: headings occur first as
`row_kind=context`, remain joinable, and are not annotatable. Article edits only
occur in the event timeline.


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
