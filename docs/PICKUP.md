# Pickup note — 2026-08-19

All production processes were stopped cleanly. Work is on
`feat/wikidisputes-ssot`; commit `1725800` is the implementation baseline.
There are intentional uncommitted fixes after that commit: streaming article
Parquet output, persistent API connections, lossless heterogeneous Arrow
schemas, separate DRN filing/accepted-mediation/closure events, and one new
regression test. Ruff, mypy, and 22 tests pass.

Current checkpoints:

- Exact source projection/audits and the 2001–2007 WikiConv years are complete.
- The resumable 2008 download remains at
  `data/staging/wikiconv/wikiconv-english-2008.zip.part`; do not delete it.
- MediaWiki has 360 cached successful requests. The full article-history table
  is not complete; the currently exported table is only the one-page pilot.
- About 51 GiB was free at shutdown. Large data/output remains ignored.
- Gold has not been sought, opened, or migrated and remains strictly out of scope.

Resume the two independent network stages (separate terminals are fastest):

```bash
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot wikiconv enumerate
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot mediawiki hydrate-article-histories
```

Both reuse exact caches/checkpoints and require authorized network access. After
they finish, copy the observed 2008–2018 archive hashes from the annual manifests
into `config/wikiconv_archives.yaml`, then continue with:

```bash
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot wikiconv merge
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot events-dv
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot rehydrate
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot mediawiki hydrate-selected
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot representations
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot mediawiki hydrate-selected-parses
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot review-packet
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot export
UV_CACHE_DIR=.cache/uv uv run --offline wikidisputes-ssot validate
```

Run Ruff/mypy/pytest again, execute the deterministic rerun/hash comparison,
update `docs/PLAN.md` and final compact reports, review/commit the remaining
diff, then push and open a draft PR. GitHub CLI authentication was invalid at
initial inspection, so verify authentication before claiming delivery.
