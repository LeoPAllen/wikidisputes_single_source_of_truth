# Pickup note — 2026-08-19 (resumed)

Work is on `feat/wikidisputes-ssot`; the latest committed baseline is `8170b9b`.
The production jobs described below were resumed from their checkpoints on
2026-08-19. Ruff, mypy, and 28 tests pass.

Current checkpoints:

- Exact source projection/audits and the 2001–2009 WikiConv years are complete.
- WikiConv 2010 is in progress under `data/staging/wikiconv/`; do not delete its
  `.part` file. The rolling stage will remove the complete ZIP automatically.
- Full article-history hydration is in progress; its atomic temporary Parquet
  file is expected and must not be deleted while the process runs.
- About 48 GiB was free at the latest check. Large data/output remains ignored.
- Gold has not been sought, opened, or migrated and remains strictly out of scope.

If a future interruption stops either independent network stage, resume it with:

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
