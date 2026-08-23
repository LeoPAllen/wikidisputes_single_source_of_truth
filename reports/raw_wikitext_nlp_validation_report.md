# WikiDisputes SSOT: Raw-Wikitext Recovery Validation

## Executive summary

- Reconstructed population remains **133,098 unique substantive utterances** and **133,223 dispute-level utterance occurrences**; the text enrichment does not alter population, identity, outcomes, reply topology, or chronology.
- Historical raw Wikipedia comment text was recovered with **high confidence for 106,308/133,223 occurrences (79.8%)**. The remaining **26,915** observations retain the prior WikiDisputes text rather than being imputed.
- **103,395 annotation texts changed** relative to the pre-recovery export. All non-high-confidence observations changed in **0 cases**.
- High-confidence matching is strong: median normalized match similarity **1.000**, 5th percentile **0.995**, median best-vs-second-candidate margin **0.687**.
- There are **0 known-time chronology inversions** and **0 nonempty-to-empty text regressions** after enrichment.

## Method

1. Use the Wikipedia revision component of each WikiConv/WikiDisputes action ID to retrieve the corresponding historical talk-page revision.
2. Segment raw revision wikitext into candidate signed talk-page comments.
3. Treat the WikiConv character offset as a positional hint, not a literal raw-text boundary.
4. Strip MediaWiki markup **only for matching**, then compare each candidate with the known WikiDisputes utterance.
5. Promote a candidate only when the normalized match is high-confidence and clearly separated from competing candidates; ambiguous/review/unavailable cases remain unchanged.
6. Preserve the complete raw comment as archival SSOT evidence, but use a **signature-stripped raw-wikitext body** for annotation so substantive links and formatting are restored without adding routine usernames/timestamps from signatures.

## Information restored in annotation-visible text

| Feature | Before | After | Added occurrences | Rows gaining feature |
|---|---:|---:|---:|---:|
| Wiki links | 184 | 106,110 | +105,930 | 28,065 |
| User / user-talk links | 3 | 26,982 | +26,980 | 2,361 |
| Wikipedia policy links | 14 | 21,237 | +21,226 | 13,011 |
| Bracketed external links | 48 | 23,991 | +23,944 | 10,635 |
| URLs | 4,369 | 31,177 | +26,888 | 11,323 |
| MediaWiki templates | 845 | 12,093 | +11,278 | 5,341 |
| `<ref>` tags | 0 | 2,300 | +2,300 | 636 |
| Revision/diff references | 4 | 8,192 | +8,188 | 2,765 |

- Utterances containing a URL or `<ref>` increased from **2,465 to 13,596**; **11,181** utterances newly expose this reference information.
- Unique URL targets: **3,776 → 20,875**, including **17,428 newly recovered unique URLs**.
- Unique wiki-link targets: **127 → 28,241**, including **28,116 newly recovered targets**.
- Unique user-related targets newly recovered in annotation bodies: **2,241**.
- Unique Wikipedia-policy targets newly recovered: **2,821**.

## NLP / textual-preservation validation

- After stripping MediaWiki syntax for comparison, **55,427/106,308 (52.1%)** of promoted comments have exactly the same normalized visible text as before; the remainder include visible material that had previously been stripped or other small reconstruction differences.
- Median multiset token Jaccard similarity between old and recovered visible text: **1.000**; 5th percentile **0.222**; 1st percentile **0.012**.
- Promoted comments below token Jaccard 0.95 / 0.90 / 0.80: **15,094 / 13,299 / 11,524**.
- Large visible-text contraction (<80% of prior length): **2,086** rows; expansion (>120%): **10,281** rows.

## Signature-control validation

The archival SSOT retains complete historical comment wikitext, including signatures, but the annotation representation removes the final signature cluster. This avoids making routine speaker signatures an artificial predictor.

- Raw recovered comments contain **106,308** detectable UTC signature timestamps; promoted annotation bodies contain **0**.
- Signature stripping removed **172,534 user/user-talk link occurrences across 105,187 comments** while preserving user links occurring inside the substantive comment body.

## Remaining risk / limitations

- **26,915 occurrences (20.2%) are not promoted** because the match was review-level, unresolved, lacked a viable signed-comment candidate, or the historical revision was unavailable. They retain the prior WikiDisputes representation.
- High-confidence matching is algorithmic rather than manual; conservative thresholds and best-vs-second-candidate margins reduce false matches but cannot prove every extraction is perfect.
- Raw wikitext restores source-faithful links/templates rather than rendered HTML; templates and historical link targets may therefore require interpretation by downstream models.
- Restoring previously stripped evidence can legitimately change LLM classifications. This is the intended measurement improvement, but prompt/model validation should therefore be rerun on the migrated Gold set before full-corpus coding.
- Recovery coverage may be non-random because deleted/unavailable revisions and difficult comment structures are more likely to remain unrecovered; recovery status should be retained for sensitivity checks.

## Accomplishments

- Preserved the official WikiDisputes source representation unchanged while adding a richer, versioned historical MediaWiki representation.
- Recovered links, policy references, usernames/user-page references, diff links, templates, and citation markup that had been removed by WikiDisputes/WikiConv text cleaning.
- Prevented uncertain recovery from contaminating the corpus by promoting only high-confidence matches.
- Removed routine signatures from annotation-visible text while retaining them in archival evidence.
- Kept population, stable IDs, chronology, reply structure, and researcher-only outcomes unchanged.
- Gold migration continues to match **438/438 rows exactly once**, providing a stable bridge to human re-annotation.

## Reproducibility artifacts

- `output/silver/mediawiki_raw_comment_recovery.parquet`: extraction/match evidence.
- `output/silver/mediawiki_raw_comment_representations.parquet`: archival raw + body representations.
- `output/annotation/wikidisputes_llm_annotation_input.csv`: enriched annotation input.
- `reports/raw_wikitext_nlp_validation_summary.json`: machine-readable validation metrics.
- `reports/raw_wikitext_change_examples.csv`: high-information before/after examples.
