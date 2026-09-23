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
Canonical utterance chronology is ordered by known `created_at_utc`; exact ties
use numeric creation revision/position and stable source order. Creation time uses
the identified MediaWiki creation revision first, corrected WikiConv creation
time second, and Europe/London-normalized WikiDisputes source time tied to an
authoritative creation identity third. A single defensibly localized explicit-UTC
historical signature is the final creation-evidence fallback. Ambiguous DST folds,
multiple signatures, unsigned/autosigned notices, and missing evidence remain
unresolved. See `CHRONOLOGY_AND_REPLIES.md` for identity, fallback, and
diagnostic semantics. Rebuild acceptance reports after code changes so their
metadata matches the code and outputs being validated.
To rebuild affected local outputs without acquisition or recovery, run `rehydrate` and then
`export` before regenerating annotation artifacts.
Adjudicated malformed discussions are excluded only from Gold through
`config/decisions/annotation_exclusions.json`; authoritative SSOT outputs retain them.

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
  --staged-annotation output/annotation/wikidisputes_llm_annotation_input.method_b_staged.csv
uv run wikidisputes-ssot validate --config config/ssot.example.yaml
uv run wikidisputes-ssot annotation export --gold /path/to/gold_input.xlsx
```

The Gold input is the original 20-column annotation shell. The export adds only `provenance` and
writes the canonical CSV, research key, Gold workbook, and manifest under `output/annotation/`.
Compact generated reports use only `output/reports/`.

### Gold review triage (2026-09-22 audit)

Direct historical-text review of the 38 `needs_rereview` Gold rows in
`gold_input_ssot_annotation_ready(10).xlsx`. This records the audit's
dispositions, not automatic permission to clear review flags. References
below use dispute ID and `substantive_order`, except where an exact
`utterance_id` is supplied.

**Retain — 30 rows:** D00003 (2, 6); D00111 (4); D00181 (22);
D00977 (5); D01057 (3, 6, 9, 11 × 3, 13, 18, 21, 22, 24, 26, 28, 32);
D01342 (17); D03503 (9, 17); D03977 (4, 5, 12, 13);
D05465 (8); D05549 (13, 27); D06530 (11).

These are defensible contributions, including valid short replies,
multi-paragraph single turns, source-backed speaker repairs, and the
three-part D01057 split (Still → Belchfire → Still). D01342:17 and
D03503:9 are false-positive speaker conflicts; retain the current
speakers. D00181:22 remains creation-time unresolved. Previously
annotated rows affected by changed attribution or splitting still
require human re-review.

**Further work — eight rows:**

- D00003 / `181458312.3149.3149`: correct speaker to BlastOButter42.
- D01342 / `260624906.75956.75956`: restore omitted marked quotation.
- D01493 / `308287895.17875.17875`: `tag` is a section heading, not speech; retain as context, not an annotation unit.
- D03503 / `715133338.29482.29482`: resolve original IP attribution and remove unsigned-comment boilerplate.
- D05549 / `133430871.30166.30166`: verify original speaker and text; WikiDisputes fallback is provisional.
- D05549 / `133975276.36783.36783`: restore strike-through, apology, and explanation; check historical-text policy.
- D06315 / `606024046.22544.22544`: split SAS81's request from Guy/JzG's separately authored response; original fallback is also merged.
- D07626 / `662046446.113863.113863`: restore missing addressee and repair extraction.

These findings concern the September 22 audited outputs. Recheck against
subsequent rebuilds before changing review states or Gold membership.

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
