# WikiDisputes single source of truth

An evidence-preserving, versioned command-line pipeline for the WikiDisputes
source projection, complete selected-conversation WikiConv reconstruction, and
candidate computation-driven outcomes. It never rewrites released source fields
and never treats missing historical evidence as a negative outcome.

Requirements: Python 3.12 or 3.13 and
[uv](https://docs.astral.sh/uv/). Large inputs and outputs are kept under ignored,
configurable roots. The annual WikiConv scan uses a rolling download/filter/delete
strategy so peak disk use is one annual ZIP plus selected records.

```bash
UV_CACHE_DIR=/tmp/wikidisputes-uv-cache uv sync --locked
UV_CACHE_DIR=/tmp/wikidisputes-uv-cache uv run wikidisputes-ssot full-run \
  --config config/ssot.example.yaml
```

Resume the same content-addressed/checkpointed run with:

```bash
UV_CACHE_DIR=/tmp/wikidisputes-uv-cache uv run wikidisputes-ssot resume \
  --config config/ssot.example.yaml
```

The authoritative machine contracts are
[`schemas/tables.yaml`](schemas/tables.yaml) and
[`schemas/acceptance_matrix.yaml`](schemas/acceptance_matrix.yaml). See
[`docs/RUNNING.md`](docs/RUNNING.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
and [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) before interpreting
the outputs. Gold workbooks and annotation databases are intentionally not inputs.

## Annotation-ready exports

After a completed SSOT run, `scripts/build_annotation_inputs.py` creates the outcome-blind full LLM annotation CSV and can migrate the existing Gold workbook onto SSOT identities and chronology. This is a downstream consumer step and does not rerun rehydration. See `docs/ANNOTATION_EXPORTS.md`.
