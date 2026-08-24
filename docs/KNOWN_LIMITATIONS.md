# Known limitations

- Raw historical comment recovery and annotation promotion are separate. A
  high-confidence V3.3 match can still be rejected by the promotion-safety gate.
- Review, unresolved, no-candidate and unavailable cases retain the prior
  WikiDisputes/SSOT annotation representation rather than being guessed.
- Recovery coverage may be non-random because unavailable revisions and unusual
  talk-page structures are harder to recover; recovery status should therefore
  be retained for sensitivity checks.
- Comment extraction and promotion safety are algorithmic. Ordered-content,
  critical-token, boundary, signature, neighbor and matcher-evidence checks
  reduce but cannot eliminate error. Ambiguous candidates deliberately fall back,
  so safe markup recovery is conservative and may have false negatives.
- Full signatures are retained archivally but removed from annotation bodies to
  avoid introducing routine speaker/timestamp information as predictors.
- The current canonical Gold input is a calibration shell rather than a completed
  coded Gold set: its coder and evidence-span fields are unpopulated. The 15
  held high-confidence candidates and 37 unresolved/no-candidate recoveries need
  human adjudication if raw historical text is required; trusted fallback is used
  meanwhile.
- 1,455 substantive logical utterances still lack defensible creation timestamps.
