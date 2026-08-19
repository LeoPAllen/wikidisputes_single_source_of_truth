# Source lineage

Authoritative current source:

- repository: `https://github.com/christinedekock11/wikidisputes`
- commit: `b6f5906179724969a883ebf3012064765edf133e`
- `data.tar.gz`: `52544dd6ae28963cf77c39069a8e043d89e3cfa297acae584ad82e81c881ad4d`

Historical article-edit enrichment:

- commit: `9a82336b4a493f6aeddd89ca2edf56dd2dd41ca5`
- `data.tar.gz`: `dd3b56a68d1d1766e8e5bf3f5f510362ca5d1a5eff2c34f8cbc560f6378e5252`
- removal commit: `04d3593c659b91570a95f873b86c366116c01acf`

Non-authoritative reference only:

- `sampled_data.json.zip`:
  `f3db3613680ff79d0a7bdd453a58a193da0a73a4c69cb073ba69ddc9cefd081b`

The full current archive reproduces 217/4,441 escalated discussions/rows,
9,006/133,019 non-escalated discussions/rows, and 99,502 original, 30,722
modification, and 7,236 restoration rows. The historical pre-removal source adds
4,504 exact edit/edit-summary records.

WikiConv is read from Cornell's English annual corpora for 2001–2018. The upstream
files have no immutable release tag; this project pins the byte size and observed
SHA-256 for every annual ZIP in `config/wikiconv_archives.yaml` and retains exact
selected JSON lines plus annual manifests. Observed retrieval date is 2026-08-18.

Primary documentation reviewed:

- WikiConv corpus documentation: `https://convokit.cornell.edu/documentation/wikiconv.html`
- WikiConv paper: `https://aclanthology.org/D18-1305/`
- Revisions API: `https://www.mediawiki.org/wiki/API:Revisions`
- Parse API: `https://www.mediawiki.org/wiki/API:Parsing_wikitext`
- Compare API: `https://www.mediawiki.org/wiki/API:Compare`
- Wikimedia API etiquette: `https://www.mediawiki.org/wiki/API:Etiquette`

Exact retrieval paths, sizes, hashes, decoding status and request evidence are in
the `source_manifests`, `source_files`, annual manifests and MediaWiki request
manifests. Retrieval timestamps do not enter canonical content hashes.
