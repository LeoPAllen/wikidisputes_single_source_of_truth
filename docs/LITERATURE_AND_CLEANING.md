# Literature and cleaning lineage

The machine-readable authority is
`literature/cleaning_registry.yaml`; the pipeline materializes it as
`silver/literature_cleaning_registry.parquet`. Searches were performed on
2026-08-18 across the official repositories, ACL Anthology full PDFs, author
publication evidence, backward citations, forward/exact-name web searches and
public scholarly indexes. Subscription-only index coverage was unavailable and
is recorded as a coverage limitation, not silently treated as exhaustive.

The upstream WikiConv paper establishes that conversation history includes
creation/addition, modification, deletion and restoration. De Kock and Vlachos
(2021) describe tag-to-WikiConv and DRN-to-WikiConv alignment, their paper-era
filters, and concurrent edit-summary augmentation. The paper population differs
from the current repository release and is never substituted for it.

Vasilets et al. (2024) use the current-release row counts. Their cleaning order
and exact published attrition are registered as a derivative replication: the
canonical SSOT never adopts their >1,000-word deletions, duplicate/dangling-row
deletions, incomplete-discussion deletion, or same-author/same-addressee merge.
The replication bundle includes row-level flags and a separate consecutive-run
merge-candidate view; the published manual disagreement/addressee step remains
unreproducible without annotations and is not approximated as canonical truth.
They explicitly report that user-inserted links are not visible in the released
dataset, which motivates revision-level link recovery rather than treating the
released plain text as lossless markup.

De Kock and Vlachos (2021, survival regression) and De Kock, Stafford, and
Vlachos (2022, WikiTactics) are additional confirmed WikiDisputes uses. Their
filtering/sampling rules become named derivative flags/views only. In particular,
WikiTactics' approximately balanced sample must not be interpreted as population
prevalence. No Gold workbook or annotation database was sought, accessed, or
inspected.
