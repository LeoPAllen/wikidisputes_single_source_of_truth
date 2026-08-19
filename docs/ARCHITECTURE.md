# Architecture and data flow

The pipeline separates evidence from interpretation:

```text
pinned WikiDisputes archives ──> Bronze exact bytes/ranges ──> source_projection
                                      │                            │
pinned WikiConv years ──rolling filter│──> selected actions ──> logical lifecycle
                                      │                            │
historical release / MediaWiki ───────┴──> events/representations  │
                                                                   v
                         Silver entities/bridges ──> canonical Parquet exports
                                                      + rebuildable DuckDB views
```

Bronze is append-only. WikiDisputes archives and extracted members are retained
byte-for-byte. Selected WikiConv JSON lines and conversation metadata are retained
exactly, while each full annual ZIP is deleted only after a verified selected-data
manifest is atomically committed. MediaWiki request/response bodies use a
content-addressed blob store with immutable per-request manifests.

Silver tables express identity, lifecycle, evidence, availability, and uncertainty.
Canonical Parquet files are deterministic functions of pinned inputs, canonical
configuration, and code. Retrieval/run timestamps appear only in provenance
fields/manifests and are excluded from deterministic canonical content hashes.
The DuckDB file is a convenience catalog over Parquet and is rebuildable; the
Parquet bundle and blobs are authoritative. Large event/sensitivity exports,
article histories and wikitext recovery are streamed in bounded page/response
batches rather than materialized wholesale in memory.

The three universes are never conflated:

- `source_projection` reproduces the released 137,460 rows exactly.
- `full_rehydrated_thread` unions every recoverable WikiConv logical creation in
  the 9,218 selected conversation IDs and retains lifecycle evidence.
- `analysis_episode` is an explicit episode-membership relation with versioned
  cutoff, eligibility, observation, and candidate DV state.

Stages use atomic file replacement and annual/request success manifests. Schema,
identity, representation, join-contract, and DV versions are independent so a
change can invalidate only the appropriate derived layer.
