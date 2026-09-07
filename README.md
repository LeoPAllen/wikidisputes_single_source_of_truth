# WikiDisputes single source of truth

This repository contains one evidence-preserving Python package and CLI for rebuilding the
WikiDisputes structural SSOT, recovering validated historical comment text, and exporting
outcome-blind annotation artifacts.

Requirements: Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run wikidisputes-ssot full-run --config config/ssot.example.yaml
uv run wikidisputes-ssot revision-diff hydrate --config config/ssot.example.yaml
uv run wikidisputes-ssot revision-diff recover --config config/ssot.example.yaml \
  --baseline-evidence output/silver/method_b_recovery_evidence.parquet
uv run wikidisputes-ssot revision-diff select --config config/ssot.example.yaml
uv run wikidisputes-ssot annotation export --gold /path/to/gold_input.xlsx
uv run wikidisputes-ssot validate --config config/ssot.example.yaml
```

Recovery hydration is cache-first and does not use the network unless `--allow-network` is
explicitly supplied. Do not casually rerun the expensive recovery corpus.

The three principal deliverables are:

- canonical structural SSOT: `output/canonical/wikidisputes_episode_utterances_ssot.parquet`
- full annotation corpus: `output/annotation/wikidisputes_llm_annotation_input.csv`
- hand-annotation Gold: `output/annotation/gold_input_ssot_annotation_ready.xlsx`

Generated products and compact validation reports live under `output/`; resumable state lives
under `checkpoints/`; immutable downloaded evidence lives under `data/bronze/`. See
[`docs/RUNNING.md`](docs/RUNNING.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and
[`docs/RECOVERY_VALIDATION.md`](docs/RECOVERY_VALIDATION.md).
