# Running and resuming

## Setup and inputs

```bash
uv sync --locked
```

Copy `config/ssot.example.yaml` if local roots or network identity must differ. Acquire the pinned
WikiDisputes inputs with `source download`, verify them with `source verify`, and retain exact
downloaded evidence under `data/bronze/`.

## Canonical SSOT

```bash
uv run wikidisputes-ssot full-run --config config/ssot.example.yaml
uv run wikidisputes-ssot resume --config config/ssot.example.yaml
```

The authoritative structural result is
`output/canonical/wikidisputes_episode_utterances_ssot.parquet`.

## Validated historical text recovery

```bash
uv run wikidisputes-ssot method-a recover --cache-only
uv run wikidisputes-ssot method-a additive-fallbacks
uv run wikidisputes-ssot method-a promote
uv run wikidisputes-ssot revision-diff hydrate --config config/ssot.example.yaml
uv run wikidisputes-ssot revision-diff recover --config config/ssot.example.yaml \
  --baseline-evidence output/silver/method_b_recovery_evidence.parquet
uv run wikidisputes-ssot revision-diff select --config config/ssot.example.yaml
```

Hydration is cache-only unless network access is explicitly authorized. Resume accepted evidence
as immutable controls. Checkpoint shards under `checkpoints/revision_diff/recovery/` are reusable;
exact response evidence under `data/bronze/` and `data/cache/` is not disposable scratch space.

## Validation and annotation exports

```bash
uv run wikidisputes-ssot revision-diff invariants \
  --config config/ssot.example.yaml \
  --staged-annotation output/annotation/wikidisputes_llm_annotation_input.csv
uv run wikidisputes-ssot validate --config config/ssot.example.yaml
uv run wikidisputes-ssot annotation export --gold /path/to/gold_input.xlsx
```

The Gold input is the original 20-column annotation shell. The export adds only `provenance` and
writes the canonical CSV, research key, Gold workbook, and manifest under `output/annotation/`.
Compact generated reports use only `output/reports/`.

## Quality checks

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run pytest -q
uv run wikidisputes-ssot --help
uv run wikidisputes-ssot annotation --help
uv run wikidisputes-ssot revision-diff --help
```
