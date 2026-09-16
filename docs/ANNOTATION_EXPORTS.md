# Annotation exports

Annotation is a downstream, outcome-blind view of the immutable structural SSOT. Final text
selection is monotonic: validated Method A, then accepted Method B, then trusted source fallback.
The full source-occurrence CSV contains all 137,460 source rows, including all 4,237
context-classified rows. `context` describes provenance/type only; every source row is
annotation-eligible, and context rows are not assigned logical utterance identities.

```bash
uv run wikidisputes-ssot annotation export --gold /path/to/gold_input.xlsx
```

The command verifies `config/decisions/method_b_validation_decision.json`, applies the frozen
combined representation, and writes the canonical CSV, research key, annotation-ready Gold, and
manifest beneath `output/annotation/`.

The Gold input contract is exactly 20 existing annotation-facing columns. The output preserves
their names and order and adds exactly one column, `provenance`. Engineering `ssot_*` and
`*_legacy` fields are rejected. Physical rows are deterministic: `dispute_sequence` ascending,
then source rows by explicit `display_order`. Every source row has the codable annotation-facing
role `utterance`; no dispute is required to contain a context row or to start with one. Canonical
context IDs and descriptive context provenance (`ssot_context_node_uid` and
`ssot_row_provenance`) remain preserved in SSOT engineering fields, but
do not control annotation eligibility, source-row retention, or Gold ordering. `chronology_rank`
is nullable and reports real creation order only; it is never filled from display order. The
exporter does not recalculate chronology. The Gold/sample workbook is a separate downstream view;
it contains sampled rows and does not define source-occurrence coverage. Only discussions listed
in `config/decisions/annotation_exclusions.json` are omitted;
they are not replaced to preserve a fixed sample size. Every substantive text continues to equal
the accepted `method_b_combined_representation.parquet` selection, and the full annotation CSV
contains no outcome columns. Its `timestamp` field contains creation time only; raw source and
action timestamps remain separately named evidence fields. A context-classified source occurrence
uses the Gold provenance `wikidisputes_source`: it is codable exact source text, not a Method-A/B
promotion and not an annotation-only context scaffold.

The migrated engineering workbooks are not annotator-facing artifacts.
