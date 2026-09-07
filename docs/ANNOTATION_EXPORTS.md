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
one context row first per dispute, then substantive rows by the existing numeric
`utterance_order`. The exporter sorts on canonical order; it never recalculates that field. The
frozen validation contract is:

- 438 rows: 404 substantive and 34 context
- provenance: 320 `method_a`, 58 `method_b`, 26 `method_a_fallback`, 34 `context`
- every substantive text equals `method_b_combined_representation.parquet`
- no outcome columns in the full annotation CSV

The migrated engineering workbooks are not annotator-facing artifacts.
