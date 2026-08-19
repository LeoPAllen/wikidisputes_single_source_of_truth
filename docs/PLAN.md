# WikiDisputes SSOT Implementation Plan

Updated: 2026-08-19

## Repository findings

- Repository root: `/Users/leopallen/Documents/wikidisputes_research_CODEX_WIDGETS/wikidisputes_single_source_of_truth`
- Starting commit: `2dac64d`; starting branch/upstream: `main` / `origin/main`.
- Feature branch: `feat/wikidisputes-ssot`.
- Starting tree was clean and contained only `README.md`; `data/` and `scripts/`
  were empty. No `AGENTS.md`, contribution guide, architecture, environment,
  tests, schemas, or prior pipeline code exists.
- Remote: `git@github.com:LeoPAllen/wikidisputes_single_source_of_truth.git`.
- GitHub CLI account `LeoPAllen` is configured but its token is invalid.
  Network/DNS was unavailable inside the default shell sandbox. Local work is
  unaffected; push/PR is a known delivery risk requiring reauthentication.
- Runtime: macOS, 8 logical CPUs, Python 3.13.1, `uv` available; approximately
  57 GiB free at inspection. Full 18-year WikiConv and Wikipedia-history storage
  may exceed local capacity and must be measured before acquisition.
- Gold inputs are prohibited and will not be searched for or inspected.

## Binding decisions

The full implementation contract is in `docs/SSOT_REQUIREMENTS.md`; executable
gate mappings are in `schemas/acceptance_matrix.yaml`. The three universes,
representation vocabulary, immutable source evidence, identity/content-hash
separation, candidate DV semantics, censoring states and no-consensus claims are
non-negotiable. External unavailability blocks only affected gates and cannot be
converted into a pass or completeness claim.

The implementation will use Python with `uv`, PyArrow/Parquet, DuckDB, Pydantic,
Typer, httpx, and pytest/Ruff/mypy unless source inspection reveals an
incompatibility. Large data lives under ignored configurable roots. Canonical
content excludes retrieval timestamps/run IDs so identical pins/config/code are
hash stable.

## Phases and gates

1. **Inspection and contracts — complete**
   - Inspect repository, auth, disk/runtime, official repositories/docs/papers,
     pinned source objects and available WikiConv distribution sizes.
   - Create requirements, acceptance matrix and this maintained plan.
2. **Environment and foundation — complete**
   - Locked project, configuration, schemas, content-addressed blob store,
     canonical serialization, atomic/checkpoint stage runner, manifest model.
3. **Source projection — complete**
   - Download/verify both authoritative archives and derivative audit archive;
     byte-range preserving JSON extraction; exact audits and Parquet projection.
4. **Rehydration and evidence — in progress**
   - Stream all annual corpora for selected IDs; logical lifecycle/contexts;
     targeted polite MediaWiki revision/parse/compare cache; links/signatures.
5. **Reconciliation and canonical views — implemented; production input pending**
   - Identities/aliases, reply graph/chronology, duplicate and five cross-label
     fixtures, episodes, historical article/tag/formal events.
6. **DVs and literature replications — implemented; historical coverage remains candidate**
   - Four separate candidate DV families, censoring/evidence and named
     noncanonical cleaning replication views; stratified review packets.
7. **Exports and validation — implemented; final production execution pending**
   - Required Parquet/DuckDB bundle, manifests/dictionaries/reports; every matrix
     gate receives pass/fail/blocked/human-required evidence.
8. **Execution — in progress**
   - Pilot, intentional bounded interruption/resume, repeated determinism run,
     then full production to the extent pinned sources are obtainable.
9. **Delivery — pending production completion**
   - Diff/secret/size review, coherent commits, push, draft or ready PR, verify
     actual commit and URL. Invalid GitHub auth is tracked, not presumed fixed.

## Commands run

```text
pwd
git rev-parse --show-toplevel
git branch --show-current
git status --short --branch
git remote -v
git rev-parse --abbrev-ref --symbolic-full-name @{upstream}
find .. -name AGENTS.md -print
rg --files (repository documentation/environment inventory)
df -h .
getconf _NPROCESSORS_ONLN
python3 --version
command -v uv poetry gh
gh auth status
git log --oneline --decorate -12
git ls-files -s
git ls-remote --heads origin
find (local input/artifact inventory)
git switch -c feat/wikidisputes-ssot
uv lock
uv sync --locked
wikidisputes-ssot source verify/extract/project/audit
wikidisputes-ssot reconstruct
wikidisputes-ssot source historical-edits
wikidisputes-ssot lineage/literature/literature-replicate/events-dv
wikidisputes-ssot pilot
wikidisputes-ssot wikiconv enumerate
ruff format/check; mypy; pytest
```

Primary web sources opened on 2026-08-18:

- official WikiDisputes GitHub repository;
- ConvoKit WikiConv documentation;
- Hua et al. (2018) ACL Anthology record/full-text link;
- MediaWiki Revisions, Parsing wikitext, and Compare API documentation.

Search logs and document-level inclusion/access findings will be stored in the
literature registry rather than inferred from this narrative.

## Current blockers and resolutions

- **GitHub authentication:** invalid `gh` token. Resolution pending user/system
  reauthentication; local implementation continues. Push/PR may be blocked.
- **Default-shell DNS:** resolved for public retrieval through the authorized
  external-network path. The CLI itself records/retains exact failures.
- **Capacity:** measured annual compressed total is approximately 27 GiB and the
  largest one-year archive approximately 2.53 GiB. Implemented rolling annual
  download/filter/checkpoint/delete, so only one full year exists at a time.
- **Historical archive retrieval:** a server ignored an attempted unsafe resume;
  that partial was quarantined. The exact Git object was then extracted from the
  pinned repository commit and verified against the binding hash.
- **Interruption/resume:** the 2005 WikiConv stage was deliberately interrupted
  after its verified archive download and before its success manifest. Resume
  reused the hash-verified archive, completed selected extraction, wrote the
  manifest, deleted the annual ZIP, and continued with 2006.
- **Disk minimization:** canonical duplicate exports use hard links where the
  filesystem permits; API bodies use deterministic gzip in content-addressed
  storage; annual WikiConv archives and temporary extracted metadata are removed
  after each verified selected-row checkpoint. Full annual ZIP retention is
  opt-in only.
- **Article-history pilot:** one resolved article page was hydrated over its
  prespecified 365-day window using metadata/SHA-1 only: 132 revision observations,
  no retrieval failures. The deterministic DV materializer produced observed
  7/30/90-day negative/positive states with window and revision evidence.
- **Partial full-rehydration integration:** an isolated 2001–2006 run completed,
  exercising context/action separation, lifecycle flattening, reply repair,
  episode membership and required Parquet writes. Its temporary 1.3 GiB output
  was removed after inspection.
- **2026-08-19 resume:** the annual sweep resumed from the retained 2008 byte
  range after 2001–2007 completed. Article hydration resumed from exact cached
  responses with a streaming Parquet writer, persistent HTTP connections and a
  bounded four-worker page pool (about four requests/second aggregate); the
  two-page concurrency pilot recovered 246 revision observations with no
  failures. The later per-revision parse stage uses the same bounded/windowed
  request pattern and preserves deterministic revision order.
- **Heterogeneous-table regression:** a production dry-run showed that Arrow's
  mapping inference could omit fields appearing only on later event kinds.
  Writers now normalize the ordered union of fields, with a regression test;
  DRN filing, accepted-mediation evidence and closure are separately typed.

## Completion record

Current verified results:

- current archive/hash/count gates pass exactly: 9,223 discussions, 137,460 rows,
  99,502 original, 30,722 modification, 7,236 restoration;
- full byte-addressable source projection: 137,460 rows, 110,518,570 bytes,
  SHA-256 `4675d84cdb94aaa9dda595b3c6a485621d1cd3e34506cadadc58c1926504ccac`;
- all fixed audits match: 137,334 unique IDs, 126 repeated rows, 19,486 raw
  dangling replies, 11,451 after original-ID aliasing, 164 child-before-parent,
  7,826 equal-time edges, 8,392 tied discussions and zero adjacent inversions;
- all five cross-label IDs are detected and quarantined;
- historical pre-removal enrichment recovered 4,504 exact article-edit records;
- deterministic pilot contains 155 rows and repeated with byte-identical SHA-256
  `02555e3fe7c75c5e2b01a0cc76e5142178b79336d3b4de4638961b340953cbd5`;
  Ruff, strict mypy and 23 tests pass;
- annual WikiConv production enumeration completed 2001–2007 and is actively
  resuming 2008 using bounded disk.

Not complete until the annual sweep, final reconciliation/export, repeated hash
check, acceptance report, Git review/commit and attempted push/PR are finished.
