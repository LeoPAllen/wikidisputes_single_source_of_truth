# Known limitations

- Raw historical comment recovery is high-confidence for 106,308 / 133,223
  utterance occurrences (79.8%).
- Review, unresolved, no-candidate and unavailable cases retain the prior
  WikiDisputes/SSOT annotation representation rather than being guessed.
- Recovery coverage may be non-random because unavailable revisions and unusual
  talk-page structures are harder to recover; recovery status should therefore
  be retained for sensitivity checks.
- Comment extraction is algorithmic: normalized text similarity and a strong
  best-vs-second-candidate margin substantially reduce but cannot eliminate
  matching error.
- Full signatures are retained archivally but removed from annotation bodies to
  avoid introducing routine speaker/timestamp information as predictors.
- 1,455 substantive logical utterances still lack defensible creation timestamps.
