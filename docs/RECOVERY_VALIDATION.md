# Recovery validation record

Accepted production recovery covers 106,375 of 133,223 substantive occurrences (79.8473%):
87,312 Method A, 19,063 Method B, and 26,848 trusted fallbacks. Fallback rows remain complete
corpus rows; they are not missing data.

| Artifact | SHA-256 |
| --- | --- |
| `output/silver/method_b_recovery_evidence.parquet` | `a09e642fc3b39d0629cc64f7efb0c102c9c37b4ce5e0422fc20159c577a29040` |
| `output/reports/revision_diff/method_b_selection_audit.parquet` | `a76aa7a30abf016c87a5cf04bc844ad93fe5a66a4df2e808deb8aec625c31f57` |
| `output/silver/method_b_combined_representation.parquet` | `4c73a10da85a2c98defbc8c7df0644c006e506abfae75712e611b41e0d8f781f` |
| `output/annotation/wikidisputes_llm_annotation_input.csv` | `19828c7aad0ccc719a3567755ba032c9fbc276619b7ceb0bddf171617db0678d` |

Stage-7 validation established unique source identities, byte-identical Method-A controls, no
unsafe Method-B selections, unchanged population/order/chronology/replies, and no outcome leakage.
The human acceptance record is `config/decisions/method_b_validation_decision.json`.

Closed decision-support work is summarized here:

- DiscussionTools parsed 200/200 pilot revisions, agreed with trusted control boundaries for only
  2/40 controls, and yielded 0/160 safe residual recoveries; it was not adopted.
- Residual-ceiling sampling found no defensible relaxation of assignment, lifecycle, or boundary
  gates.
- Rule probes supported only the narrowly validated X1 cases; production X1 added nine manually
  inspected recoveries. Broader rules were rejected.
- Pilot comparisons and LLM audit bundles supported those decisions but are not production
  dependencies.
