# Utterance fidelity diagnosis

Status: read-only diagnosis of the September 22 rebuild. No generated artifact
was edited for this investigation. The IDs below are reproducible examples,
not proposed special cases for the pipeline.

## Finding

There is **more than one failure mode**. An immutable WikiDisputes/WikiConv
*source row*, a revision-diff *action*, and a physical *signed comment* can
cover different spans. The current boundary extractor works backward from a
terminal signature and stops at a heading, template, indentation change, or
previous candidate ([`boundaries.py`](../src/wikidisputes_ssot/revision_diff/boundaries.py)).
This can leave quotations or earlier paragraphs outside a candidate. The
assignment and safety gates then reject it or select only the later span.
Other cases lack original-author evidence or contain retrospective unsigned
attribution; a boundary change cannot resolve their speakers.

The annotation/Gold projection largely carries these upstream uncertainties.
The [turn-integrity decisions](../src/wikidisputes_ssot/turn_integrity.py)
retain implicated rows for review. The rebuilt corpus has 9,693 blockers:
8,772 merged-comment *signals*, 869 speaker conflicts, 51 strong replay
identity cases, and one missing-speaker case. These are screening counts,
not 9,693 proven malformed comments. Among the merged-comment blockers,
4,675 have no recorded merged preceding paragraph: a revision changed span
crossing comments can itself nominate a coherent row. The September 22 Gold
triage found retainable rows among these signals. Blanket repair or flag
clearance would be unsound.

## Evidence and prospects by case

| Case | Recorded evidence and present failure | Prospect |
| --- | --- | --- |
| D01342 `260624906.75956.75956` | Cached revision `260624906` places a marked `{{blockquote|...}}` between the comment opening and Jerem43's terminal signature. Source and staged text omit it. The structural parser emits only offsets 84028–84154 (52-character body). Method B reports `b_no_candidate`, unmatched assignment, and `changed_span_not_in_one_comment`. Parent `260624782` is not cached. | **Likely repairable** if a bounded span from a unique source anchor to the signature includes the balanced quotation and excludes the prior signed comment. Missing parent limits action-diff proof; retain review until source-to-span mapping is validated. |
| D03503 `715133338.29482.29482` | Revision actor is SineBot. The apparent `138.49.1.3` signature is a later autosigned attribution template; `-24.197.253.43` also appears in prose. The earliest cached revision containing the comment is the SineBot revision, and parent `715133243` is absent. The parser isolates text but cannot prove original authorship. | **Attribution blocked by present evidence.** A verified predecessor/creation revision or independent origin record is needed. Do not equate the bot, template target, or IP-like prose with the original author. Boilerplate could be separated while preserving an attribution blocker. |
| D05549 `133430871.30166.30166` | Source text is 805 characters, staged text 722, and the cached target has the fuller unsigned contribution. Source speaker is `192.169.41.37`; revision actor is `192.169.41.40`. No terminal author signature is present. Method B has no candidate; parent `133430208` is absent. | **Text may be recoverable; speaker is unproved.** Exact fallback still needs a demonstrated single physical span. Targeted parent evidence could establish whether the actor created or only edited it. Keep attribution blocked. |
| D05549 `133975276.36783.36783` | Staged text is 105 characters; source combines 3,049. Cached revision has a signed strike-through/retraction at 08:50 on 27 May and a later signed apology/explanation at 01:12 on 28 May. The parser emits the first 164-character body and only the final 810-character body of the later comment, losing intervening paragraphs. A broad source fallback crossed the first signature and was rejected. Parent `133816860` is absent. | **Potentially repairable as two evidenced comments**, using markup-aware boundaries and derived units under the existing source UID. A one-row fallback is unsafe. Both units require human re-annotation; original creation revisions remain unverified. |
| D06315 `606024046.22544.22544` | Source/staged row (805 characters) merges SAS81 and Guy/JzG. Cached revision `606024046` yields two existing parser candidates: SAS81 at 23600–24248 and JzG at 24249–24576, each explicitly signed. Method B assigns only JzG and flags the source row as crossing comments. Parent `606023721` is absent. | **Likely repairable with the existing split mechanism** after proving both spans map uniquely to this source row. The target proves two physical units but not SAS81's creation revision. The existing D06315 fixture concerns another source row. |
| D07626 `662046446.113863.113863` | Source/staged text starts after the addressee. Cached target `662046446` contains the linked addressee and full GPRamirez5-signed comment; cached predecessor `662027471` lacks it. The extractor emits only offsets 119184–120210 (971-character body), losing earlier paragraphs. Recovery reports `b_review` and `changed_span_not_in_one_comment`. | **Strong repair prospect.** Target/predecessor evidence can validate the whole newly introduced signed span and restore the addressee through a general boundary-extension rule. Exclude prior signed material and competing candidates. |
| D05215 `677534539.19050.1517` | Source speaker is blank and the final row has `unresolved_missing_speaker`. Yet cached revision `677534539` contains a unique matching comment with a terminal PeterDaley72 user-link signature; the existing parser emits offsets 19441–20405. Revision actor Sam Sailor is a later editor. This source UID has no Method B recovery row. | **Likely repairable** by a general missing-speaker proof joining a unique source occurrence to its historical signature. The actor must not replace the signer. |
| D06910 repeated rows | Cached revision `65956163` contains two verbatim Ruchiraw-signed occurrences at separate page offsets (2269 and 9025), with different source action coordinates. | **Keep both with unresolved identity.** Equality and a common revision do not prove an alias; these could be a repost or copied material. |

## Why the safeguards matter

The revision-diff gate rejects `changed_span_not_in_one_comment`, contested
assignment, and unproved modification continuity rather than promoting a
nearby signature. This prevented wrong-speaker and cumulative-text repairs.
A preliminary broad “source starts with staged text” fallback matched 121
absorbed rows; several crossed signatures or unsigned templates. The accepted
rule requires one exact, unique, terminally signed source comment and applies
to only two unrelated rows. Source length or prefix overlap is not proof of
one physical comment.

Existing [`split_units()`](../src/wikidisputes_ssot/turn_integrity.py) derives
stable child IDs from source UID and part index. No new identity system is
needed. A repair does need exact cached spans, explicit speaker evidence for
each child, non-overlap with neighboring source rows, and human re-annotation
when text, speaker, or unit identity changes. The original source record must
remain immutable.

## Feasible next investigation

1. Build a **read-only span evaluator** over cached target revisions. From a
   unique source anchor, collect content through its terminal signature,
   treating earlier signed comments and headings as hard stops. Handle
   blockquote templates, blank paragraphs, indentation, and strike-through
   without silently truncating a comment.
2. Compare each proposed span with the existing Method B candidate, source
   record, neighboring source rows, and predecessor when cached. Emit an
   evidence record and abstain on multiple plausible mappings or absent
   signatures.
3. Route one proven span through existing recovery; route multiple proven
   signed spans through `split_units()`. Preserve source provenance and require
   Gold re-annotation for changed units.
4. Keep original-author investigation separate. Targeted acquisition of
   missing parent revisions may resolve D03503 and D05549's first row, but
   current cached evidence does not. Do not infer those speakers from a later
   editor or autosigned template.

This is a feasibility assessment, not proof that one evaluator would safely
repair every row. Regression checks should include the audited IDs, retainable
Gold units, and negative controls with nested quotes, templates, reposts, and
unsigned neighbors. Correct abstentions matter as much as successful repairs.

## Reproduction sources

- [Running audit](RUNNING.md), “Gold review triage (2026-09-22 audit).”
- `output/canonical/wikidisputes_source_projection.parquet`, keyed by
  `wikidisputes_id_exact` and `source_row_uid`.
- `output/silver/method_b_recovery_evidence.parquet`, keyed by `source_row_uid`.
- `data/cache/mediawiki_revision_content.sqlite`, `revision_cache` table.
- `output/silver/turn_integrity_decisions.parquet` and
  `output/annotation/wikidisputes_llm_annotation_input.csv`.
