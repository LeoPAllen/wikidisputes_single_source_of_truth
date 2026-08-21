# Annotation-ready exports

The completed SSOT can be consumed without rerunning rehydration or modifying canonical SSOT artifacts.

Run `uv run python scripts/build_annotation_inputs.py --gold /path/to/gold_input.xlsx`.

The command creates:

- `output/annotation/wikidisputes_llm_annotation_input.csv`: full, outcome-blind annotation input. Source WikiDisputes observations are collapsed to one logical utterance, ordered by SSOT creation chronology, with context rows retained.
- `output/annotation/wikidisputes_annotation_research_key.csv`: researcher-side episode/escalation key. Do not pass this file to the annotation model.
- `output/annotation/gold_input_ssot_migrated.xlsx`: non-destructive Gold migration. Legacy `utterance_id` values and existing columns are preserved while SSOT identities, chronology, display text, and reply ordering are added/applied.
- `reports/annotation_export_report.json`: lightweight full-export checks.
- `reports/gold_ssot_migration_report.json`: lightweight Gold-migration summary.
- `reports/gold_ssot_migration_changes.csv`: row-level list of order/text/reply changes and re-review flags.

## Text policy

Annotation text comes from `wikidisputes_annotation_display.parquet`. For recovered utterances this uses the exact WikiConv final text; otherwise it falls back to exact WikiDisputes text. The exact WikiDisputes source text is retained separately in `ssot_source_text_exact`.

This restores information available in the recovered representation, including links that may be absent from the WikiDisputes projection, without destroying the source-exact field.

## Chronology policy

Logical utterances are ordered with the SSOT `utterance_order`, which is based on recovered creation/addition chronology. Modification timestamps do not create new conversational turns.

## Gold migration policy

Gold is matched through the SSOT annotation join contract. The migration stops rather than guessing when a Gold row does not resolve exactly once.

Within each existing Gold dispute, the same selected rows are reordered according to SSOT logical chronology. The legacy application `utterance_id` is retained, so this step does not intentionally change the annotation application's storage key.

Rows whose displayed text, relative order, or reply order changes are marked `ssot_needs_rereview=TRUE`.
