# Representations, links, signatures, and actors

Representation kinds follow the binding vocabulary in `SSOT_REQUIREMENTS.md`.
Exact WikiDisputes and WikiConv strings are preserved before any interpretation.
Historical `action=parse&oldid=` output is always
`rendered_html_reconstructed`, with endpoint, parameters, parser options,
retrieval time, warnings and exact response hash. It is never called original or
archival HTML.

The full parse hydrator is independently checkpointed by request hash. Wide
tables store response/blob pointers, content hashes, byte counts and JSON pointers;
they do not duplicate full page wikitext or reconstructed HTML inline. Exact API
response bytes remain the retrieval evidence.

The link extractor emits only explicit wikilink targets or syntactically valid
literal URLs. Anchor words never generate inferred targets. Each occurrence keeps
raw markup/URL, target, normalized target, display anchor, kind, spans where
defensible, representation/version, method and evidence. Revision recovery can
therefore show that a target absent from `wikidisputes_text_exact` existed in
revision wikitext without altering the source field.

Signature parsing records explicit User, User talk, contributions and timestamp
evidence. `not_observed_in_fragment` is not missing authorship. WikiDisputes user,
WikiConv speaker, revision actor and signature target remain separate until an
evidenced identity resolution is made. Hidden/deleted/IP/temporary/renamed and
mismatch states are explicit.
