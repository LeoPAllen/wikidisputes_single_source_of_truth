# Known limitations

- Raw historical comment recovery and annotation promotion are separate. A
  high-confidence V3.3 match can still be rejected by the promotion-safety gate.
- Method-A compatibility fallbacks are deliberately incomplete. Historical
  signatures are considered only when canonical parsing yields no candidates;
  certified source-artifact cleanup is comparison-only; and frozen legacy
  geometry is merely a candidate hypothesis subject to all current gates. These
  constraints favor false negatives over changing an existing safe recovery.
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
- The annotation-ready Gold workbook is a text-selection contract, not evidence that annotation
  coding has been completed. Trusted fallback remains explicit in `provenance`.
- 1,363 substantive logical utterances still lack defensible creation timestamps.
- Method B currently requires independently defensible signature-led raw comment
  boundaries; genuinely unsigned/malformed comments remain unresolved.
- Method-B restoration proof is a bounded prior-history exact-body persistence
  index, not full WikiWho. Edited restorations remain review.
- Large ambiguous revision assignments and pathological exact-diff traces fail closed under
  explicit operational limits. Accepted Method-B yield and validation are frozen in
  `RECOVERY_VALIDATION.md`; no broader recoverability claim is made.
