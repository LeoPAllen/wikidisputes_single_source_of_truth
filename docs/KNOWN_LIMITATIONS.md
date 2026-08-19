# Known limitations

- Cornell's WikiConv ZIPs have no immutable release tag. Annual observed hashes
  pin the 2026-02-26 files actually used; a future upstream replacement is a new
  source observation, not an implicit update.
- Historical revision wikitext, parse HTML, page moves, actor/signature matching,
  full formal-venue history, tag recurrence and article SHA-1 follow-up require
  targeted MediaWiki/dump hydration. A view remains incomplete or not-observable
  until coverage is present; current source evidence is never substituted.
- WikiDisputes text omits link targets noted by Vasilets et al. Revision recovery
  may disagree with released visible text because of version or boundary changes.
- Vasilets et al.'s exact tokenizer/code and manual disagreement selection are not
  available. The named replication view uses documented reversible flags and
  reports its count divergence; it does not alter canonical data.
- Five conversation IDs occur under both source labels. They remain quarantined
  from binary analysis pending episode-specific formal/tag/thread evidence.
- The 2012–2018 common-support export is a sensitivity view, not a claim that the
  source population or a future balanced annotation sample has that prevalence.
- GitHub CLI authentication was invalid at initial inspection. Push/PR status is
  reported from actual commands and never inferred.
