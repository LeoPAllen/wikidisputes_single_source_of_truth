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

## Raw MediaWiki annotation enrichment

Historical talk-page revisions can be recovered after canonical SSOT construction.
Recovery confidence only identifies a candidate. A separate conservative,
markup-aware safety gate compares that candidate with trusted evidence for the
same source occurrence before annotation promotion. The released WikiDisputes
fields remain immutable; rejected candidates and their diagnostics remain
auditable while annotation falls back to the trusted representation.

The frozen V3.3 recovery run reports candidate coverage separately from safe
promotion coverage; the latter must be computed by
`scripts/promote_raw_mediawiki_comments.py` after recovery completes.
Both recovery and promotion verify that their exact source-occurrence targets
still match the canonical annotation join contract before producing output.

An additive, independent revision-to-revision reconstruction channel (Method B)
is available under `wikidisputes-ssot revision-diff`. It is cache-first, groups
multiple actions at revision level, has its own conservative safety decision, and
cannot change annotation exports without an explicit validated Stage-6 rebuild.
The same command group includes an isolated 200-row DiscussionTools feasibility
pilot whose rendered evidence is never consumed by production selection, plus a
weighted, resumable audit for estimating the remaining recoverability ceiling. See
[`docs/REVISION_DIFF_RECOVERY.md`](docs/REVISION_DIFF_RECOVERY.md).
