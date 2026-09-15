# Annotation exports

Annotation is a downstream, outcome-blind view of the immutable structural SSOT. Final text
selection is monotonic: validated Method A, then accepted Method B, then trusted source fallback.
Context rows remain unchanged.

```bash
uv run wikidisputes-ssot annotation export --gold /path/to/gold_input.xlsx
```

The command verifies `config/decisions/method_b_validation_decision.json`, applies the frozen
combined representation, and writes the canonical CSV, research key, annotation-ready Gold, and
manifest beneath `output/annotation/`.

The Gold input contract is exactly 20 existing annotation-facing columns. The output preserves
their names and order and adds exactly one column, `provenance`. Engineering `ssot_*` and
`*_legacy` fields are rejected. Physical rows are deterministic: `dispute_sequence` ascending,
one context row first per dispute, then substantive rows by canonical `utterance_order`, whose
primary key is known `created_at_utc`. Numeric creation revision/position only breaks exact-time
ties; unknown times use the deterministic fallback defined by the canonical chronology contract.
The exporter sorts on canonical order; it never recalculates that field. The
Gold contains one row per sampled logical utterance plus one context row per retained sampled
discussion. Only discussions listed in `config/decisions/annotation_exclusions.json` are omitted;
they are not replaced to preserve a fixed sample size. Every substantive text continues to equal
the accepted `method_b_combined_representation.parquet` selection, and the full annotation CSV
contains no outcome columns.

The migrated engineering workbooks are not annotator-facing artifacts.
