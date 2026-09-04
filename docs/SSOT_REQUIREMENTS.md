# WikiDisputes SSOT: Binding Implementation Requirements

Status: binding implementation contract  
Captured: 2026-08-18  
Scope: this repository; Gold annotations are expressly out of scope.

## Purpose and universes

The pipeline MUST produce a reproducible, versioned, evidence-preserving single
source of truth for WikiDisputes. It distinguishes three universes:

1. `source_projection`: every row and field in the pinned release, in byte and
   source order preserving form.
2. `full_rehydrated_thread`: every recoverable WikiConv context node and
   utterance lifecycle action in every selected conversation, including records
   absent from WikiDisputes.
3. `analysis_episode`: an explicitly eligible episode/window view with versioned
   index, predictor, outcome, applicability, observation, and censoring rules.

“All utterances” means all recoverable logical CREATION/ADDITION comments in the
selected conversations, not unrelated talk-page material and not merely released
WikiDisputes rows. A completeness claim is forbidden unless all selected
conversations are enumerated or each gap has an evidence-backed status.

## Pinned lineage and fatal integrity gates

The authoritative current input is
`christinedekock11/wikidisputes@b6f5906179724969a883ebf3012064765edf133e`,
whose `data.tar.gz` SHA-256 MUST equal
`52544dd6ae28963cf77c39069a8e043d89e3cfa297acae584ad82e81c881ad4d`.
Before derivation it MUST contain 217 escalated discussions / 4,441 rows and
9,006 non-escalated discussions / 133,019 rows: 9,223 discussions and 137,460
rows total, split into 99,502 `original`, 30,722 `modification`, and 7,236
`restoration` records. A mismatch blocks downstream production for that input.

Historical enrichment is pinned to commit
`9a82336b4a493f6aeddd89ca2edf56dd2dd41ca5`, archive SHA-256
`dd3b56a68d1d1766e8e5bf3f5f510362ca5d1a5eff2c34f8cbc560f6378e5252`.
Commit `04d3593c659b91570a95f873b86c366116c01acf` documents later article-edit
removal. Article edits/edit summaries from the historical release are separate
events, not utterances. `sampled_data.json.zip` (SHA-256
`f3db3613680ff79d0a7bdd453a58a193da0a73a4c69cb073ba69ddc9cefd081b`)
is derivative audit material only.

Every record carries repository, commit, archive and file hashes, file, source
class/side, case identifier/index, row index, extraction provenance, and schema
version. Releases are never merged implicitly.

## Immutable evidence and representations

Bronze is append-only and content-addressed. It retains downloaded archives,
extracted files, exact JSON record byte slices (or verified byte-range pointers),
complete raw objects, WikiConv artifacts, exact API response bodies, request
manifests, wikitext, parse/compare responses, hashes, error bodies, and explicit
availability/suppression states. Derived layers never overwrite Bronze.

Silver exact fields are not stripped, trimmed, normalized, entity-decoded beyond
JSON decoding, markup-removed, username-normalized, or repaired. Derived values
use separately named fields. Portable canonical JSON uses UTF-8, sorted object
keys, compact separators, JSON booleans/numbers, and explicit `null`; the
versioned immutable projection has fixed field order and test vectors.
Non-standard source numeric tokens such as bare `NaN` remain exact in source
bytes; derived portable JSON represents them as an explicit
`{"$wikidisputes_nonfinite_float":"NaN"}` tag rather than conflating them with
null or emitting invalid JSON.

Representation vocabulary is exact:

- `wikidisputes_text_exact`: decoded release field;
- `source_record_bytes`: exact JSON record bytes;
- `revision_wikitext_raw`: pinned-revision API/dump wikitext;
- `utterance_wikitext_fragment`: evidenced extraction from that revision;
- `rendered_html_reconstructed`: later parsing of a pinned revision;
- `visible_text_reconstructed`: text derived from documented rendered output;
- `html_archival`: only independently archived historically served bytes.

Current `action=parse` results MUST NOT be called original HTML. Exact response
bytes and exact returned strings precede any DOM normalization. Each
representation records hash, bytes, encoding/MIME, revision, spans when
defensible, method/version, parser/options, retrieval/evidence, confidence,
availability/suppression, `available_at`, and `leakage_class`.

## Storage and canonical entities

Use content-addressed Bronze blobs, relational Parquet Silver/canonical tables,
and DuckDB views/database. Large content is referenced by stable blob hash and
metadata. Large inputs, outputs, caches, secrets, and HMAC keys are ignored.

Machine-readable schemas MUST cover: `source_manifests`, `source_files`,
`source_rows`, `selected_conversations`, `conversation_source_membership`,
`disputes`, `dispute_episodes`, `episode_threads`, `context_nodes`, `utterances`,
`utterance_actions`, `utterance_versions`, `utterance_representations`,
`source_id_aliases`, `identity_registry`, `reply_edges`, `authors_actors`,
`signatures`, `links`, `article_revisions`, `events`, `event_evidence`,
`outcomes`, `dv_definitions`, `annotation_join_contract`, `quality_flags`, and
`literature_cleaning_registry`. Explicit bridges preserve many-to-many
relationships. Unknown, ambiguous, not-observed, censored, hidden, suppressed,
and not-observable states are not collapsed to null/zero.

## Stable identity and later annotation joining

`source_row_uid` is derived only from pinned lineage and immutable original
location (repository/commit/archive/file/side/case/index). It is independent of
text, sort order, repairs, export row, and annotations.

`logical_utterance_uid` prefers `wikiconv:<creation-or-ancestor-id>`. Its fallback
is a versioned deterministic ranking of immutable aliases/anchors, with method,
version, candidates, confidence and adjudication status. It never uses mutable
text/canonical order. The identity registry is append-only; promotions retain
old fallbacks as versioned aliases/redirects. Each action/version and context
node has its own stable key. Context is displayable/joinable but not annotatable.

`source_id_aliases` retains every current/original/ancestor/parent/action ID,
namespace, occurrence, competing resolution and validity state. The versioned
`annotation_join_contract` exposes source/logical/context/action/version,
dispute/episode/conversation/thread IDs, all aliases, exact source text/user
hashes, canonical selected-text hash, portable projection hash, original
location/order, canonical/display order, reply target, evidence pointers and all
schema/algorithm versions. Gold is never sought, opened, inspected, or migrated.

## Reconstruction, lifecycle, and chronology

Scan pinned English WikiConv 2001–2018 annual corpora, union selected
conversation IDs, retain supplying corpus/year, and deduplicate only identical
action identity. Cross-year loss/double-counting and missing conversations are
reported. Every record carries source/full-thread/episode/predictor/outcome flags,
recovery status/method/evidence/confidence, availability time, and leakage class.

CREATION/ADDITION establishes a logical utterance; MODIFICATION/DELETION/
RESTORATION normally becomes an action/version of that utterance unless evidence
proves a new turn. Recover creation, pre-first-reply, cutoff, final and exact
source states where evidence permits. Never fabricate unavailable historical
states. Validate lifecycle cycles, parentage, branches and delete/restore
consistency; preserve unresolved branches.

Chronology and reply structure remain separate. Required views are logical
creation timeline, context display timeline, full event timeline, and exact
source-order views. Order logical utterances by: creation time, numeric creation
revision, numeric ID position, original source index, logical UID. Equal times
receive a simultaneity group; this tie-break does not imply causal order. Context
has display order but no utterance order. Article edits are events.

Reply resolution preserves raw and repaired targets, target UID/order, method,
evidence, confidence/status/reason, depth/root/lag/indentation/addressee and
child-before-parent/equal-time flags. It traverses all ID aliases and action
history; unresolved targets remain explicit; cycles/self-references are flagged.

## Links, authors, actors, and signatures

Recover link evidence actively from pinned revision wikitext/parse output; never
invent a target from anchor text. Each occurrence records raw markup/URL, raw and
normalized target, anchor, kind, source/revision, defensible spans/DOM path,
source presence, revision recovery, method/version, confidence/ambiguity and
evidence.

Keep source user, WikiConv speaker, revision actor name/ID, raw signature,
signature HTML/display name, User/User talk/contributions targets and timestamp
separate. Store actor/signature match evidence and optional resolved identity.
Unsigned, renamed, IP, temporary, deleted, hidden, bot, mismatch and suppression
states are explicit. Missing signature is not missing authorship. Exact public
identities remain authoritative; optional HMAC pseudonyms require an external
secret and never enter canonical hashes.

## MediaWiki hydration

Prefer pinned dumps for bulk data and use Action API for targeted evidence. The
client uses an identifying configurable User-Agent, polite bounded concurrency,
batching/continuation, `maxlag`, gzip, `Retry-After`, exponential jittered retry,
content-addressed exact responses, normalized request/response hashes,
timestamps/status/error bodies, atomic checkpoints, resume and no repeated work.
It requests supported revision ids/flags/timestamps/user/userid/size/sha1/content
model/comment/content/tags/roles with `rvslots=main`, and distinguishes missing,
deleted, revdeleted and oversight-suppressed states. Compare and parse requests
record all parameters, parser/skin/options, warnings, endpoint and metadata.
Stable page ID and title-at-event/move/archive evidence are retained.

## Reversible reconciliation and fixed audits

Canonical data never deduplicates on text alone, deletes long comments, merges
same-author turns, or drops a discussion due to one defect. Repairs are mappings,
relationships, flags or named derivative views with contributors, candidates,
evidence and reasons. Splits require defensible source spans and never inherit
annotations automatically. Historical toxicity is preserved, not recomputed.

Pre-repair audits target: 137,334 unique current IDs; 126 repeated-ID rows caused
by five cross-label discussions; 19,486 raw dangling reply targets; about 11,451
after simple original-ID aliases; 164 resolvable child-before-parent cases; 7,826
equal-time reply edges; 8,392 discussions with ties; zero simple adjacent array
timestamp inversions. Exact, normalized and near-duplicate reports and field
round-trip tests are required.

The mandatory cross-label fixtures are `514025327.9787.9787`,
`545620272.7408.7408`, `509266297.83325.83325`,
`512260994.112001.112001`, and `502277435.16708.16708`. Preserve all evidence.
Resolve one verified formal episode as positive with contradictory provenance;
split distinct episodes into non-overlapping windows; otherwise quarantine from
binary analysis. One episode cannot have contradictory analytic outcomes.

## Episodes, events and dependent variables

Episode rows carry source selection, boundaries, thread/source-projection end,
versioned index/cutoff, observation start/end/horizon, tag/formal events, threads,
page identity/title-at-event, component alignment evidence/status, applicability,
observation/censoring/unknown states. Raw events are never removed by analytical
cutoffs; pre/concurrent events are labeled. Cutoffs derive from sampling and
episode rules, never future outcomes. If no primary formal horizon is established,
publish prespecified 30/90/365-day and full-follow-up sensitivities and mark the
primary choice pending.

Each separate DV stores semantic/build/index versions, applicability, observed
value/time/horizon, observed/censored/unknown/not-observable state, evidence,
method/confidence, candidate/validated status and adjudication fields. Missing is
not zero. Computational definitions remain candidate until humans adjudicate the
specified review gate.

- Formal escalation: venue-specific DRN/accepted mediation/RfC/3O/ANI/AN/
  mediation/protection/arbitration/other events, times and 30/90/365/full views.
- Durable tag clearance/recurrence: versioned template families/scope, first
  removal, 30/90 absence, 30/90/365 re-addition and next same-scope tag.
- Revert stability: deterministic SHA-1 identity reverts plus corroborating tags,
  7/30/90-day any/count/time/participant/mutual/section and article sensitivity.
- Formal-process closure: process-conditional raw closure and representation,
  normalized success/failure/general/withdrawn/other/unknown and time/evidence.

No composite resolution score is allowed. Non-escalation, tag removal,
inactivity, low revert activity, length/duration, exit, any edit, LLM consensus or
semantic proposal adoption are not primary resolution outcomes.

## Temporal/sampling safeguards and exports

Predictor-safe views exclude future text/events and validate `available_at` /
`leakage_class`. Preserve post-reply/post-cutoff modification flags, era/year
distributions, 2012–2018 common support, duplicate leakage checks and grouping
keys for episode/thread/page/conversation/participant. Preserve sampling; do not
fit the main model or claim balanced annotation prevalence.

Required exports are:

- `wikidisputes_source_projection.parquet` (exactly 137,460 rows);
- `wikidisputes_utterances_ssot.parquet` (one row/logical creation/addition);
- `wikidisputes_episode_utterances_ssot.parquet` (one row/episode-membership);
- `wikidisputes_annotation_display.parquet` (typed context/utterance rows);
- complete raw-event and separately versioned DV tables/views.

The normalized bundle/blobs are authoritative. All exports retain stable
evidence links, statuses and hashes and are documented by manifests.

## Operation, validation and delivery

One typed, locked CLI provides download/verify, extraction, source audit,
literature registry/views, WikiConv hydration, MediaWiki revision/parse/compare,
version reconstruction, link/signature extraction, reply repair, episodes,
events, DVs, validation, export, full run and resume. Stages are idempotent,
content-addressed, dependency/config/schema-version aware, atomic, bounded,
streaming where practical, checkpointed and deterministic apart from retrieval
provenance. Pilot, intentional interruption/resume, repeat-hash check and full
production are executed; unavailable sources block only affected coverage with
exact evidence.

Primary literature and documentation are read from pinned/recorded URLs. Search
queries, dates, inclusion decisions and access failures enter a machine-readable
registry. Published destructive cleaning becomes flags and named replication
views only, including the Vasilets et al. >1,000-word, duplicate/dangling-parent,
incomplete-discussion and consecutive same-author/same-addressee sequence.

Validation is executable and mapped in `schemas/acceptance_matrix.yaml`.
Stratified review packets include population/seed/sample IDs, exact evidence,
schema and instructions; packet creation is not adjudication. Compact reports,
schemas, tests, fixtures, lockfile, config, lineage, architecture, dictionaries,
identity/join, representations, chronology, episodes/DVs, limitations, manifests,
reconciliation, QC, and clean/resume instructions are committed. Raw/generated
data and secrets are not.

The task closes only after diff review, coherent commit(s), push and verified PR
when authentication/network permit. A draft PR is required while implementable
gates fail or human validation remains pending, with blockers stated precisely.
