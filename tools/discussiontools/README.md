# Pinned DiscussionTools parser harness

This is a self-contained CLI harness around the **real** PHP implementation in
MediaWiki's DiscussionTools extension. It does not render wikitext, and it does
not contain a Python or local reimplementation of comment parsing. Historical
raw wikitext remains the archival source of truth; the supplied historical
Parsoid HTML is only rendered-structure evidence.

The container creates only an empty, ephemeral SQLite `interwiki` table so
MediaWiki can resolve link prefixes without a production wiki database. It
contains no page, revision, user, or discussion data.

Build the pinned image:

```sh
docker build -t wikidisputes-discussiontools:rel1_46-pinned tools/discussiontools
```

Inspect the exact software report (including the installed Composer Parsoid
package version):

```sh
tools/discussiontools/run.sh --version
```

Parse one or many newline-delimited JSON records. Each input record must have
`revision_id`, `title`, and `html`. Output is exactly one NDJSON result per
input record; parse failures are explicit `discussiontools_error` rows so a
resumable caller can checkpoint them instead of silently dropping work.

```sh
printf '%s\n' '{"revision_id":201674796,"title":"Talk:Example","html":"<p>Example -- <a href=\"./User:Example\">Example</a> 12:34, 1 January 2020 (UTC)</p>"}' \
  | tools/discussiontools/run.sh
```

The output records include the HTML SHA-256, all pinned revisions, comment and
heading data, body/signature/timestamp DOM ranges, stable DOM-path anchors,
warning lists, and parent IDs. The parent pipeline is responsible for caching,
raw-wikitext mapping, and all fail-closed promotion gates.
The runtime container has no network, no writable root filesystem, no Linux
capabilities, and only a bounded writable `/tmp`.

From the repository root, write and run the bounded pilot with:

```sh
uv run wikidisputes-ssot revision-diff discussiontools-sample \
  --config config/ssot.example.yaml --seed 20260831
uv run wikidisputes-ssot revision-diff discussiontools-feasibility \
  --config config/ssot.example.yaml --checkpoint-every 25 --resume
# Only after inspecting cache gaps and explicitly approving retrieval:
uv run wikidisputes-ssot revision-diff discussiontools-feasibility \
  --config config/ssot.example.yaml --checkpoint-every 25 --resume --allow-network
```

The first feasibility command never fetches HTML. The second may use the exact
historical revision REST endpoint; fetched bytes and HTTP evidence are persisted
before parsing so an interruption can resume without repeating completed work.
Process-wide harness failures leave parser work pending; per-input parser errors
remain explicit terminal evidence.

Pins:

- PHP CLI base: `8.3.27-cli-bookworm`, multi-architecture manifest
  `sha256:01224f5f2e75fa43a326797d7b80552ca0bcfb37f60cbb81efdf63956b4d3fe4`
- Composer: `2.8.12`, PHAR
  `sha256:f446ea719708bb85fcbf4ef18def5d0515f1f9b4d703f6d820c9c1656e10a2f2`
- MediaWiki REL1_46: `dfd080bb34fe9160b027c814e08af29a8e63063c`
- DiscussionTools: `16fa124bcf4ad5bb9419abe634d700772bc07be8`
- VisualEditor: `020ff448040df3adac2531d22229b7629a1eb5c3`
- Linter: `df783ad77cba2ae28adb1875ce124b0f67b9758d`
