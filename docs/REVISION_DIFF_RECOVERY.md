# Revision-diff recovery (Method B)

Method B reconstructs a comment from the exact target revision and its API-provided parent. It
uses deterministic token diffing, structural boundaries, revision-global one-to-one assignment,
lifecycle checks, and a conservative safety decision. Network use is confined to
`revision-diff hydrate --allow-network`; `recover` is local and checkpointed.

Production selection is monotonic. Existing Method-A-safe text is immutable; only unresolved rows
with accepted safe Method-B evidence may select Method B; all others retain trusted fallback text.
X1 proof logic in `revision_diff/x1_proof.py` remains part of production.

```bash
uv run wikidisputes-ssot revision-diff hydrate --config config/ssot.example.yaml
uv run wikidisputes-ssot revision-diff recover --config config/ssot.example.yaml \
  --baseline-evidence output/silver/method_b_recovery_evidence.parquet
uv run wikidisputes-ssot revision-diff select --config config/ssot.example.yaml
uv run wikidisputes-ssot revision-diff invariants \
  --config config/ssot.example.yaml \
  --staged-annotation output/annotation/wikidisputes_llm_annotation_input.csv
```

Frozen evidence and acceptance are recorded in [`RECOVERY_VALIDATION.md`](RECOVERY_VALIDATION.md).
Checkpoints are reusable only when workflow, population hash, and ordered revision IDs match.
