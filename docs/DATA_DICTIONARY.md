# Data dictionary

The authoritative keys, required fields, foreign keys, enums, and null semantics
are in `schemas/tables.yaml`. This document gives the interpretive overview.

- `source_manifests`, `source_files`, `source_rows`: pinned lineage, extracted
  files, exact record byte ranges/JSON and unmodified decoded source fields.
- `selected_conversations`, `conversation_source_membership`: the WikiConv
  conversation universe selected by the source release and every source occurrence.
- `disputes`, `dispute_episodes`, `episode_threads`, `episode_utterances`: source
  records, versioned analytic episodes, thread membership and many-to-many
  utterance membership.
- `context_nodes`: WikiConv rows explicitly marked `is_section_header=true`,
  source-only heading candidates for unavailable conversations, and talk-page
  metadata context. They have `display_order` and are never annotation-eligible
  or assigned `utterance_order`.
- `context_actions`, `context_representations`: lifecycle history and exact text
  for context nodes, outside annotatable utterance/action tables.
- `utterances`: one logical creation/addition, keyed independently of text/order.
- `utterance_actions`, `utterance_versions`: creation, modification, deletion, and
  restoration evidence. Later actions do not create a new turn by default.
- `utterance_representations`: separately typed exact source text, WikiConv final
  text, revision wikitext, extracted fragments, reconstructed HTML/visible text,
  and genuinely archival HTML if it ever exists.
- `source_id_aliases`, `identity_registry`: observed ID namespaces, competing
  resolutions, append-only issued identities and future redirects.
- `reply_edges`: raw/repaired targets, resolution evidence and chronology flags.
- `authors_actors`, `signatures`, `links`: non-collapsed source user, speaker,
  actor, signature and explicit link evidence.
- `article_revisions`, `events`, `event_evidence`: historical edits plus separately
  typed tag/formal-process/lifecycle evidence.
- `dv_definitions`, `outcomes`: separate candidate DVs with applicability,
  observation/censoring state, horizon and evidence. They are not consensus labels.
- `annotation_join_contract`, `annotation_context_join_contract`: future-facing
  keys and exact source/context anchors. They have no human annotation columns
  and have never consulted Gold data.
- `quality_flags`, `literature_cleaning_registry`: reversible diagnostics and
  publication-specific derivative cleaning specifications.

`null` means structurally inapplicable or not supplied. Epistemic states belong in
status columns: `unknown`, `ambiguous`, `not_observed`, `censored`, `unavailable`,
`hidden`, `suppressed`, and `not_observable` are distinct from zero/false.
