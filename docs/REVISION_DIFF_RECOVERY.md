# Method B: revision-diff raw comment recovery

## Scope and architecture

Method A is the frozen full-page candidate matcher and its separate promotion
safety audit. Method B is an additive, independent reconstruction channel:

```text
source occurrence/action
  -> exact target revision metadata
  -> API parentid (never oldid - 1)
  -> exact target/predecessor response blobs and hashes
  -> deterministic local token diff with raw spans
  -> target structural comment candidates
  -> diff-local structural candidate pruning
  -> conservative action-specific hunk attribution
  -> revision-global action/candidate assignment
  -> lifecycle checks
  -> independent Method-B safety decision
  -> monotonic A-then-B selection artifact
```

Method B never overwrites Method-A recovery, promotion evidence, caches, or
reports. Installing the code changes no annotation export. A new downstream
annotation CSV is possible only through the explicit Stage-6 command and a
separate accepted validation-decision file.

## Frozen validated baseline (2026-09-04)

Recovery is frozen at a validated practical automated-recovery ceiling of
106,375/133,223 substantive occurrences (79.8473%). Methods A+B established
106,366 exact/raw upgrades; production X1 added 9, all manually inspected and
safely selected by Method B. The frozen corpus has 133,098 logical utterances,
4,237 context rows, and 137,460 annotation rows. IDs, population, order,
chronology, replies, outcomes, and dependent variables are frozen.

Fallback does not mean a missing corpus row: every remaining row retains its
trusted WikiDisputes source text. Further reconstruction may be possible, but
the residual lacks sufficiently strong action, lifecycle, boundary, revision,
or parser/diff evidence for safe automated promotion. The remaining 20.15% is
not claimed to be theoretically irrecoverable.

The frozen artifacts are:

| Artifact | SHA-256 |
| --- | --- |
| `output/silver/method_b_recovery_evidence.parquet` | `a09e642fc3b39d0629cc64f7efb0c102c9c37b4ce5e0422fc20159c577a29040` |
| `output/reports/revision_diff/method_b_selection_audit.parquet` | `a76aa7a30abf016c87a5cf04bc844ad93fe5a66a4df2e808deb8aec625c31f57` |
| `output/silver/method_b_combined_representation.parquet` | `4c73a10da85a2c98defbc8c7df0644c006e506abfae75712e611b41e0d8f781f` |
| `output/annotation/wikidisputes_llm_annotation_input.method_b.csv` | `19828c7aad0ccc719a3567755ba032c9fbc276619b7ceb0bddf171617db0678d` |

Workflow `method-b-workflow-v8-x1-proof` passed Stage 7: canonical join/source
keys were unique; Method-A-safe rows were byte-identical; no non-safe Method-B
row was selected; source occurrence identities and substantive population were
unchanged; provenance mismatch and outcome leakage were zero; and all immutable
annotation fields passed. Stage 6 produced the Method-B sidecar without
overwriting the original annotation CSV. The accepted human decision is tracked
at `reports/method_b_validation_decision.json`; the implementation baseline is
commit `0e5c7794371c2931765b787fda821511f6cb038f`.

The stopping evidence is deliberately conservative:

- `b_unavailable=818`: 406 fetch/cache failures and 412 unavailable targets.
  Exact-ID retry considered 82 targets; network refresh remained 79 available
  and 3 retryable, producing no gain.
- The resource-limit residual screen contained 4,496 rows, but its conservative
  unique-local subset contained zero. Assignment limits must not be relaxed.
- Creation and boundary screens (6,557 and 3,403) were heterogeneous candidate
  screens, not recoverable-yield estimates. Inspection found one- or two-character
  changed spans, action/lifecycle ambiguity, boundary defects, speaker mismatch,
  and other incompatible evidence.
- The SineBot funnel was 317 `b_review`, 317 unique assignments, 309 speaker
  matches, 312 pure insertions, 242 autosign markers, 92 insertions after the
  parsed body only, and 176 predecessor structural continuities. This is too
  small and uncertain for another production rule; structural continuity can
  link different comments by the same speaker.
- X1 required substantial implementation and validation work for only nine
  additional recoveries, demonstrating strong diminishing returns.

## Coordinate and diff model

All stored wikitext ranges are half-open Python Unicode code-point offsets
`[start, end)`. They are neither UTF-8 byte offsets nor UTF-16 offsets. Local
content hashes are SHA-256 over exact UTF-8 bytes; API SHA-1 values are retained
separately. Tokens cover the raw string exactly, including whitespace, markup,
punctuation, and Unicode. Token text is not normalized.

Alignment uses exact Myers shortest-edit processing with deterministic
deletion-first tie handling. It does not use `SequenceMatcher`, autojunk,
rendered HTML, or MediaWiki Compare as its source of truth. Each coalesced
operation retains predecessor/target token and raw-character ranges. The
operational trace bound is explicit; exceeding it yields review reason
`diff_operational_resource_limit` rather than changing algorithms.

The diff localizes change; it does not define a comment boundary. Boundary
recovery independently uses explicit signatures/timestamps, headings, blank
paragraphs, indentation, and neighboring signed comments. Full raw comments,
signature-stripped bodies, exact ranges, signature evidence, and boundary
warnings are retained. Headings, page templates, and adjacent comments are hard
boundaries. Unsigned or malformed cases currently fail closed to no-candidate or
review.

### Historical-signature compatibility fallback

Historical Wikipedia signatures use several legitimate date and user-link
layouts that predate the canonical modern signature format. Historical
recognition is compatibility-preserving and fallback-only: the original
canonical structural parser runs first, and if it finds any comment candidate
its candidate geometry and bodies are returned unchanged. Historical signature
recognition runs only when the canonical parser finds zero candidates.

The historical fallback still requires explicit nearby User/User-talk or
Special:Contributions markup and a terminal-looking timestamp. Downstream
diff localization, revision-global assignment, lifecycle checks, ambiguity
handling, and Method-B safety gates are unchanged and continue to fail closed.

Validation before adoption showed:
- 17,978/17,978 previously selectable Method-B rows retained their exact
  selected candidate intervals and bodies;
- no historical candidates were emitted on pages where the canonical parser
  already found candidates;
- 2,335/2,337 (99.9%) of the strong zero-candidate historical-signature
  frontier became structurally candidate-bearing; and
- 1,850/2,337 (79.2%) of that frontier passed the unchanged downstream
  b_safe/b_usable gates.

## Lifecycle and revision-level assignment

The local action vocabulary is `creation`, `addition`, `modification`,
`deletion`, and `restoration`.

- Creation/addition requires a target-side insertion/replacement localized to a
  coherent complete target comment.
- Modification returns the complete target-revision comment and requires an
  equal aligned region in a structurally compatible predecessor comment.
- Restoration is deletion/reintroduction, not ordinary addition. Safety requires
  a bounded, complete prior-history scan and an exact prior body hash absent from
  the immediate predecessor. Frozen speakers are not rewritten from the restoring
  revision actor.
- Deletion has no target comment. It is `b_not_applicable`; available
  predecessor-side evidence is retained without fabricating a target candidate.

Revision is not treated as synonymous with action or comment. Candidates and
indivisible change evidence are assigned globally and one-to-one within each
revision. Whole-page structural parsing is retained, but assignment receives
only candidates that overlap/contain target diff spans, differ only by structural
boundary whitespace, or are immediate structural neighbors of a change at an
ambiguous whitespace boundary. There is no page-radius search. Evidence retains
the whole-page and localized candidate counts plus the localization reasons.

For a single non-deletion action, every relevant localized span remains
available. In multi-action revisions, informative frozen source text, WikiConv
offset hints, frozen speaker/signature evidence, lifecycle compatibility, and
structural hunk overlap narrow action-specific spans only when action/candidate
support is mutually unique. Offsets never decide attribution alone, ties remain
ambiguous, and revision actor is never treated as the comment speaker. The
existing assignment action/candidate/edge/state limits are unchanged; a truly
large localized graph still fails closed.

Assignment evidence is lexicographic and target-independent first: changed span,
frozen identity where available, lifecycle, informative fragment, uncalibrated
offset hint, and signature corroboration. Exact and configured near ties remain
ambiguous. Search-size limits also return ambiguity; no greedy fallback silently
manufactures uniqueness. Revision actor, signature author, WikiConv speaker, and
WikiDisputes speaker remain separate fields.

The strict Stage-4 A1 fallback is narrower than ordinary assignment: it runs
only for an `equal_global_assignments` tie. It considers the action's existing
edges only when the candidate has a parsed signature user matching the frozen
WikiConv speaker by exact `casefold()`, every non-empty changed target span is
contained in that candidate range, and exactly one identical candidate
representation/range group remains. The group must have no substantive edge
from another action; empty/sentinel edges and offset-only edges do not contest
it because offsets remain uncalibrated. A qualifying assignment records
`a1_exact_signature_speaker_fallback`; existing assigned results and generic
assignment thresholds are unchanged.

Production reruns may receive a prior evidence artifact with
`--baseline-evidence`. Its `b_safe` and `b_usable` rows are identity-validated
against the current source population, excluded from recomputation, and copied
unchanged into the new evidence artifact. Missing, duplicate, or mismatched
control identities fail the run.

For a bounded pilot or fallback/review run, attribution still loads every frozen
local utterance action sharing each selected revision. Method-A-safe,
out-of-sample, and source-unmapped WikiConv actions therefore remain assignment
blockers even though Method B emits evidence only for the requested source
occurrences. A missing target revision ID is emitted explicitly as unavailable;
it is not silently dropped.

## Safety contract and statuses

Statuses are `b_safe`, `b_usable`, `b_review`, `b_no_candidate`,
`b_unavailable`, `b_ambiguous`, and `b_not_applicable`. `b_usable` is a
validation-only tier for candidates whose only reasons are
`structure:terminal_signature` or `structure:unsigned_signature_residue`;
it is not safe and is not eligible for Stage-5 selection. A safe result still
requires all of:

- exact source/provenance mapping;
- exact target metadata and verified API `parentid`;
- available exact target and predecessor content with response/API/local hashes;
- deterministic diff evidence;
- changed evidence inside one coherent structural candidate;
- unique, uncontested revision-global assignment;
- lifecycle consistency and defensible boundaries;
- no adjacent comment, heading, template, or page-level contamination;
- consistent signature/timestamp and calibrated offset evidence when applicable;
- predecessor/target continuity for modification;
- sufficient reintroduction history for restoration; and
- no material critical-token contradiction in an explicitly aligned informative
  fragment.

Missing evidence fails closed. Whole-target similarity is not a Method-B gate:
empty, stripped, truncated, or boundary-contaminated target strings may be
recovered from structural/diff evidence. Target text can corroborate. Critical
tokens can veto only in a fragment explicitly marked informative and aligned.
Reason codes are ordered and stored with the evidence; there is no aggregate
empirical “safety score.” Operational ambiguity parameters are versioned CLI
inputs and must not be relaxed merely to increase yield.

Boundary method v2 treats blank lines as weak boundaries. A signed comment can
merge recursively with preceding unsigned paragraphs only at identical
discussion indentation/depth, without crossing a prior signed candidate,
heading/template, incompatible depth, or the prior candidate end. Exact source
offsets and raw hashes continue to cover the selected interval.

When the established parser has no diff-local candidate, Method B may retain a
`diff_span_structural` unsigned candidate as diagnostic evidence. It starts
from every substantive action-attributed target span and expands only to the
smallest region independently closed on both sides by page edges, headings or
templates, signed neighbours, or incompatible indentation. Same-depth prose
separated only by blank space is ambiguous and fails closed. Existing parsed
candidates always take precedence, multi-action revisions are excluded, and
every substantive span must fit. Because calibration found frequent over-wide
regions, this fallback is selectable only when the non-empty frozen source body
matches the exact candidate body apart from outer whitespace; otherwise its
contamination state remains unknown and it stays review evidence.

For unresolved single-action modifications, `token_persistence_continuity` may
corroborate one predecessor candidate without using a signature, speaker,
offset, or revision actor. Every substantive target span must lie inside the
target body, an exact Myers `EQUAL` block immediately adjacent to each edit must
retain at least ten exact word tokens and forty non-whitespace characters
inside both bodies, and exactly one clean, indentation-compatible predecessor
candidate may qualify. The selected predecessor range and block metrics remain
in the evidence. Existing structural continuity still runs first unchanged.
Restoration persistence was not extended: the residual evidence did not support
an unambiguous new reintroduction rule.

## Cache, network, checkpoints, and artifacts

The existing exact-response MediaWiki client supplies rate limiting, maxlag,
retry/backoff, immutable content-addressed response blobs, request hashes, and
success manifests. Method B reads those blobs first and writes its own revision
index, pairs, history, recovery checkpoints, evidence, representations, and
reports. It does not modify Method-A SQLite data. Network use is impossible from
`recover`; only `hydrate --allow-network` can fetch missing revisions.
Cache-only hydration retains unavailable states. Explicit network mode may retry
non-content observations but does not retry content suppressed/deleted by
MediaWiki.

Important artifacts under configured output/checkpoint roots are:

- `silver/method_b_source_population.parquet`
- `silver/method_b_population_profile.parquet`
- `silver/method_b_pilot_population.parquet`
- `silver/method_b_revision_content_index.parquet`
- `silver/method_b_revision_pairs.parquet`
- `silver/method_b_revision_history.parquet`
- `silver/method_b_pilot_recovery_evidence.parquet`
- `silver/method_b_recovery_evidence.parquet`
- `silver/method_b_{pilot_,}representations.parquet`
- `reports/revision_diff/method_b_*` JSON/Parquet reports
- `manual_review/revision_diff/method_b_blinded_audit_packet.parquet`
- `manual_review/revision_diff/method_b_blinded_audit_key.parquet`
- `manual_review/revision_diff/method_b_pilot_blinded_audit_{packet,key}.parquet`
- `silver/method_b_combined_representation.parquet`
- `annotation/wikidisputes_llm_annotation_input.method_b.csv` (Stage 6 only)

The compact human acceptance record is tracked separately at
`reports/method_b_validation_decision.json`; generated artifacts above remain
uncommitted.

Recovery checkpoints are deterministic population-hash/batch shards. `--resume`
reuses a shard only when its workflow version, population hash, and exact ordered
revision IDs match. Writes are atomic.

## Bounded DiscussionTools feasibility pilot

DiscussionTools is an optional, additive feasibility experiment; it is not part
of Method-B production selection. The pilot uses historical Parsoid HTML from
the exact revision REST endpoint and a pinned container running the production
PHP `DiscussionTools.CommentParser`. Rendered structure can corroborate only an
already extracted raw-wikitext candidate. It cannot create raw boundaries,
relax the existing assignment or lifecycle gates, or modify an annotation
export.

The harness uses an empty ephemeral SQLite `interwiki` table solely to keep
MediaWiki link-prefix resolution local; it does not load a wiki content
database.

First write the deterministic, disjoint 200-row sample and build the pinned
harness:

```bash
uv run wikidisputes-ssot revision-diff discussiontools-sample \
  --config config/ssot.example.yaml --seed 20260831
docker build -t wikidisputes-discussiontools:rel1_46-pinned tools/discussiontools
tools/discussiontools/run.sh --version
```

The sample contains 40 existing A/B controls and 160 unresolved rows across
restoration, modification, creation, unsigned/malformed, prior structural
fallback, multi-action, `b_review`, and `b_no_candidate` strata. Sampling fails
if any stratum is short or if source identities are absent or duplicated.

The feasibility run is cache-only unless network access is explicitly enabled:

```bash
uv run wikidisputes-ssot revision-diff discussiontools-feasibility \
  --config config/ssot.example.yaml --checkpoint-every 25 --resume
uv run wikidisputes-ssot revision-diff discussiontools-feasibility \
  --config config/ssot.example.yaml --checkpoint-every 25 --resume --allow-network
```

Fetched HTML is content-addressed and recorded independently before parser
work. SQLite state is bound to the sample, configuration, code hashes, image
name, and verified component pins. Completed revision results are immutable on
resume; a cache miss caused only by network policy remains pending so the later
explicit network run can hydrate it.
An individual `discussiontools_error` is terminal evidence for that revision.
A process-wide container failure aborts the batch without parser states, leaving
the already cached HTML available for a later `--resume` retry.

The feasibility gate requires at least 95% overall parser success, at least 90%
in every reported subgroup with ten or more rows, at least 99% exact raw-boundary
agreement on controls, no detected or unknown contamination among proposed-safe
rows, at least ten uniquely safe unresolved rows, and at least 5% safe yield in
the unresolved sample. A passed gate is evidence to consider a separate reviewed
integration; it does not authorize promotion by itself.

Pilot artifacts are:

- `silver/discussiontools_feasibility_sample.parquet`
- `reports/revision_diff/discussiontools_feasibility_sample_manifest.json`
- `cache/discussiontools/feasibility_state.sqlite`
- `cache/discussiontools/historical_html/`
- `silver/discussiontools_feasibility_evidence.parquet`
- `reports/revision_diff/discussiontools_feasibility_report.json`

The report records component and local harness hashes, sample/state/evidence
artifact descriptors, revision/render status counts, invocation policy, subgroup
rates, contamination counts, residual failure reasons, and every gate failure.

## Residual recoverability ceiling audit

This downstream-only audit derives the current residual from the frozen
selection audit. It accounts for `b_unavailable` exactly and excludes those rows
from human review. The remaining frame is sampled deterministically with seed
`20260831`: primary allocation uses B status × lifecycle, while explicit
diagnostic cells census token-persistence rows and retain DiscussionTools
coverage. Every design cell records its population, sample size, inclusion
probability, and survey weight.

Generate the 600-row CSV/HTML bundle without rerunning recovery or selection:

```bash
uv run wikidisputes-ssot revision-diff residual-ceiling-packet \
  --config config/ssot.example.yaml --seed 20260831 --sample-size 600
```

Review `output/manual_review/revision_diff/residual_ceiling_20260831/audit.html`
and enter labels with the resumable terminal command. Each completed row is
atomically persisted to the companion CSV; enter `stop` at the recoverability
prompt or interrupt between rows to stop safely.

```bash
uv run wikidisputes-ssot revision-diff residual-ceiling-label \
  --config config/ssot.example.yaml --seed 20260831
```

After all 600 labels are complete, write the weighted class estimates,
uncertainty intervals, subgroup breakdowns, three implied coverage ceilings,
and the rule-family decision gate:

```bash
uv run wikidisputes-ssot revision-diff residual-ceiling-summarize \
  --config config/ssot.example.yaml --seed 20260831
```

The audit bundle and weighted result are generated evidence and remain
uncommitted. Neither command mutates Method-B recovery, selection, or annotation
artifacts.

For independent LLM review, generate a compact bundle from the already frozen
600-row audit. This command verifies the saved UID/order hash and survey design;
it never resamples or invokes recovery/selection:

```bash
uv run wikidisputes-ssot revision-diff llm-audit-bundle \
  --config config/ssot.example.yaml --seed 20260831
```

The generated `llm_audit_bundle/` directory contains full joined evidence in
Parquet, offset-safe compact JSONL review windows, the separately taxonomized
`b_unavailable` population, blinded calibration controls and their exact key,
and a hash-bound quality manifest. Generated bundle files remain uncommitted.

### Read-only residual rule probes

The frozen evidence can be evaluated by strict Boolean proof rules without
invoking or modifying recovery, assignment, selection, or annotation outputs:

```bash
uv run wikidisputes-ssot revision-diff residual-rule-probe \
  --config config/ssot.example.yaml --seed 20260831
```

The command verifies the frozen 600-row UID/order hash, preserves its survey
weights, and writes only diagnostic row results and a weighted summary beneath
`residual_ceiling_20260831/rule_probes/`. X1 proves an exact source-to-candidate
mapping; R1 identifies only X1-proven non-restoration resource-limit cases;
C1a probes closed signed creations; M1 probes signature-only modifications; and
B1 permits only allowlisted outer-whitespace or signature-adjacent markup
repairs. These rules produce no confidence score and make no promotion.

X1 centralizes body-identity provenance as exact, outer-whitespace-only,
colon-indentation-only, or terminal-signature-formatting-prefix. Indentation
normalization removes only a parsed leading run of `:`; `*`, `#`, and `;` are
never removed. The production X1 path uses exact identity and the validated
terminal-signature-formatting mode; other X1 modes remain probe-only.
Signature formatting qualifies only when allowlisted terminal opening tags are
paired literally with closing tags inside the independently parsed,
immediately-following signature. Internal text, substantive markup, and raw
candidate ranges remain unchanged. Frozen speaker and raw signature user are
retained separately as match/mismatch/unknown provenance; mismatch supplies no
evidence but is not an X1/R1 veto.

Production invokes the same pure identity/provenance helper after ordinary
global assignment. X1 cannot assign or disambiguate a candidate: it requires
one assigned, uncontested candidate, one action, one exact source occurrence,
clean contamination, defensible signed boundaries, and creation lifecycle. A
single changed span may begin with the immediately preceding parsed heading
only when it ends exactly at that candidate; arbitrary material outside the
candidate remains a localization failure. Machine-readable evidence records
the identity mode, speaker/signature provenance, X1 localization mode, and
proof status. Modification continuity, restoration history, assignment/search
limits, competition, lifecycle, boundary, and contamination gates remain
unchanged.
Frozen-sample runs also write the four-row `x1_signature_fragment_diagnostics.jsonl`;
it is investigation evidence and never contributes eligibility.

Retryable residual acquisition is separate from recovery. Preview the frozen
`fetch/cache failure` class without network access, then explicitly refresh it:

```bash
uv run wikidisputes-ssot revision-diff retry-unavailable \
  --config config/ssot.example.yaml --seed 20260831
uv run wikidisputes-ssot revision-diff retry-unavailable \
  --config config/ssot.example.yaml --seed 20260831 --allow-network
```

Only retryable exact-ID observations are fetched. Missing/deleted/suppressed
revisions and unknown legacy states remain fallback. A fetched response
replaces cache state only after
its requested revision ID, API `parentid`, persisted blob, response hash, and
local content hash validate. Reruns skip validated content and are idempotent;
recovery and selection criteria are unchanged.

An alternate prejoined evidence parquet can be inspected with `--input` and
`--output-directory`; it must already contain exact raw revisions, candidates,
and evidence fields. This does not construct or rerun a full residual frame.

## Staged runbook and stop conditions

The artifacts and hashes above are the baseline for downstream work. Full
recovery is expensive and is no longer a routine reproducibility step: verify
and consume the frozen artifacts unless an explicit, reviewed recovery change
requires a new run. Future experiments must supply an immutable copy of the
frozen recovery evidence through `--baseline-evidence`; accepted A/B rows are
controls, not recomputation targets.

Use the local configured paths; do not substitute hard-coded counts or IDs.

### Stage 1 — profile

```bash
uv run wikidisputes-ssot revision-diff profile \
  --config config/ssot.example.yaml
```

Inspect `method_b_population_profile.json` and its population Parquet. Stop if
canonical source provenance is nonzero, source/action mapping fails, or the
derived primary population is not exactly the then-current A fallback+review set.

### Stage 2 — bounded pilot

```bash
uv run wikidisputes-ssot revision-diff pilot \
  --config config/ssot.example.yaml --seed 20260818 --per-stratum 25
uv run wikidisputes-ssot revision-diff hydrate \
  --config config/ssot.example.yaml --pilot --max-revisions 500 \
  --batch-size 25 --history-depth 5
```

The first hydration command is cache-only. If its report has missing targets or
parents, inspect them and explicitly opt into bounded network hydration only if
appropriate:

```bash
uv run wikidisputes-ssot revision-diff hydrate \
  --config config/ssot.example.yaml --pilot --allow-network \
  --max-revisions 500 --batch-size 25 --history-depth 5
uv run wikidisputes-ssot revision-diff recover \
  --config config/ssot.example.yaml --pilot --max-revisions 500 \
  --checkpoint-every 50 --resume
```

Stop on response-hash/pointer failures, missing required predecessors, provenance
failure, unexpected population drift, or checkpoint-version mismatch. Bounded
pilot artifacts must not be described as full recovery.

### Stage 3 — pilot validation and human packet

```bash
uv run wikidisputes-ssot revision-diff validate-pilot \
  --config config/ssot.example.yaml
uv run wikidisputes-ssot revision-diff audit-packet \
  --config config/ssot.example.yaml --pilot --seed 20260818 \
  --excerpt-limit 500 --per-stratum 25
```

Inspect A/B raw-body, visible-text, left/right boundary, critical-token,
contamination, assignment, and lifecycle reports. Method A is a reference/control,
not ground truth. Text and boundary agreement rates use only rows where both
sides are present; missing/missing values are not agreements and critical-token
comparisons are unavailable when text is missing. Contamination is reported as
evaluated-clean, detected, or unknown using independently recognized signed
comment offsets; it is never defaulted to clean. Stop before Stage 4 if control
disagreements, contamination,
ambiguity, restoration evidence, or lifecycle-specific results are unacceptable.
Do not report precision/recall until adjudicated labels exist.

### Stage 4 — full primary Method-B recovery

Run only after an explicit reviewed decision to replace the frozen baseline.
First preserve and hash an immutable copy of the baseline evidence, then pass
that copy as `--baseline-evidence`:

```bash
uv run wikidisputes-ssot revision-diff hydrate \
  --config config/ssot.example.yaml --batch-size 50 --history-depth 5
uv run wikidisputes-ssot revision-diff hydrate \
  --config config/ssot.example.yaml --allow-network \
  --batch-size 50 --history-depth 5
uv run wikidisputes-ssot revision-diff recover \
  --config config/ssot.example.yaml --checkpoint-every 250 --resume \
  --baseline-evidence /immutable/copy/method_b_recovery_evidence.parquet
```

The first command profiles cache gaps without network. Stop if cache gaps are not
understood, fetch errors remain, response hashes fail, or disk/rate limits are
unsuitable. Full `recover` remains network-free and targets only current
fallback+review occurrences.

### Stage 5 — selection/backfill

```bash
uv run wikidisputes-ssot revision-diff select \
  --config config/ssot.example.yaml
uv run wikidisputes-ssot revision-diff audit-packet \
  --config config/ssot.example.yaml --seed 20260818 \
  --excerpt-limit 500 --per-stratum 25
```

Inspect predecessor/diff/candidate/B-safe/B-usable counts, fallback/review transitions,
remaining states, empty-target recoveries, lifecycle/failure yields, recovered
markup, and Method-A-safe control disagreements. Stop if any A-safe text changed
or any non-`b_safe` row, including `b_usable`, was selected.

### Stage 6 — explicit downstream rebuild

Human acceptance is separate from recovery and selection. Record it canonically
in `reports/method_b_validation_decision.json`; the frozen accepted decision
uses the following required fields (additional structured evidence is retained):

```json
{
  "method_b_accepted": true,
  "adjudicated_by": "reviewer identifier",
  "adjudicated_at": "YYYY-MM-DDTHH:MM:SSZ",
  "evidence": "path or adjudication manifest hash"
}
```

Then run:

```bash
uv run wikidisputes-ssot revision-diff rebuild-annotation \
  --config config/ssot.example.yaml \
  --validation-decision reports/method_b_validation_decision.json
```

The command writes a new `.method_b.csv`; it never overwrites the current
annotation export. Stop if human validation is incomplete or the decision file
is missing its explicit acceptance/adjudicator/time.

### Stage 7 — final invariants

```bash
uv run wikidisputes-ssot revision-diff invariants \
  --config config/ssot.example.yaml
```

This checks local source/substantive/logical/context populations, source
occurrence identity, source provenance, outcome leakage, A-safe byte identity,
selection safety, and byte-equivalence of every non-text annotation field
(covering IDs, order, chronology, reply structure, and outcomes). Any false check
is a hard stop.

Verify the frozen artifacts without running recovery or selection:

```bash
shasum -a 256 \
  output/silver/method_b_recovery_evidence.parquet \
  output/reports/revision_diff/method_b_selection_audit.parquet \
  output/silver/method_b_combined_representation.parquet \
  output/annotation/wikidisputes_llm_annotation_input.method_b.csv
```

## Known limitations

- Genuinely unsigned or malformed comments remain mostly unresolved. The
  diff-span fallback retains bounded diagnostic candidates, but exact-source
  corroboration and the normal lifecycle/assignment gates intentionally make
  automatic recovery rare.
- Historical Parsoid HTML does not itself expose raw-wikitext boundaries. The
  bounded DiscussionTools pilot therefore treats its DOM ranges only as
  rendered-structure evidence and still requires exact mapping to one existing
  raw candidate. Its evidence is not consumed by production selection.
- Defective target text is not automatically forced into a fragment alignment;
  critical-token vetoes require separately established aligned-fragment evidence.
- The exact restoration index uses bounded prior-history body hashes; edited
  reintroductions remain review.
- Large or highly ambiguous revision assignments and pathological diff traces
  fail closed under explicit operational bounds.
- WikiConv offsets are coordinates in the upstream pipeline's
  `rev_clean.clean_html` page text, not raw wikitext. Reproducing that transform
  still failed the 99.5% calibration gate on safe A/B controls, with strong
  action/year variation, so offsets remain non-decisive hints.
- No recovery yield, correctness, precision, or recall is established by the
  implementation itself. Those are later empirical results.
