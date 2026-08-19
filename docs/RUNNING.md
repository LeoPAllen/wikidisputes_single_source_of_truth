# Running and resuming

Install the locked environment:

```bash
UV_CACHE_DIR=/tmp/wikidisputes-uv-cache uv sync --locked
```

Full clean run (point the config roots at empty directories for a physically clean
run):

```bash
UV_CACHE_DIR=/tmp/wikidisputes-uv-cache uv run wikidisputes-ssot full-run \
  --config config/ssot.example.yaml
```

Exact resume command:

```bash
UV_CACHE_DIR=/tmp/wikidisputes-uv-cache uv run wikidisputes-ssot resume \
  --config config/ssot.example.yaml
```

The CLI also exposes every stage separately. `source verify` is a fatal gate.
`wikiconv enumerate` scans 2001–2018 sequentially; each verified annual ZIP is
filtered directly, exact selected records are checkpointed, then the full ZIP is
removed. `wikiconv merge` refuses to run until every annual Parquet exists.

Targeted historical evidence examples:

```bash
uv run wikidisputes-ssot mediawiki revisions --revision-id 123 --revision-id 456
uv run wikidisputes-ssot mediawiki parse --revision-id 123
uv run wikidisputes-ssot mediawiki compare --from-revision 123 --to-revision 456
uv run wikidisputes-ssot mediawiki hydrate-article-histories --max-pages 5
uv run wikidisputes-ssot mediawiki hydrate-selected --max-revisions 50
uv run wikidisputes-ssot mediawiki hydrate-selected-parses --max-revisions 10
```

Omit the bounds only for full production. Article histories retain metadata and
SHA-1 plus compressed exact API bodies, not duplicated article wikitext. Selected
talk-revision content and parse responses are content-addressed and deterministically
gzip-compressed. A bounded pilot may not be described as complete.

Use an identifying User-Agent/contact in the config. Batchable revision requests
are serialized; independent page/parse requests use the configured conservative
bounded worker pool and per-worker pacing. All requests use `maxlag`, bounded
retries, exact content-addressed response caching and success markers. Never
delete checkpoints to conceal missing coverage.

Quality commands:

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run pytest -q
uv run wikidisputes-ssot validate
```
