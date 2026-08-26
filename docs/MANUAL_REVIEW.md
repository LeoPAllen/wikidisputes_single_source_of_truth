# Manual review instructions

`wikidisputes-ssot review-packet` samples deterministic strata using seed 20260818
and writes `output/manual_review/ssot_review_packet.parquet`. It includes DV state,
reply resolution, signature/link evidence and all cross-label episodes when those
tables are available. Population counts and sampled IDs are in
`output/reports/manual_review_packet.json`.

DV strata separate definition, horizon, observation/applicability state and
observed value class (including positive/negative revert and formal-event cases).
Signature strata cross signature availability with actor-match status. Link
review includes both recovered link kinds and utterances with an observed
historical fragment from which no link was recovered.

Reviewers must inspect every cited exact evidence pointer and populate only the
blank adjudication, adjudicator, time and notes fields in a separate review result.
Required decisions include link present/absent/recovered, actor/signature match,
DV positive/negative/ambiguous/censored/not-observable, identity confidence, reply
repair, cross-label episode identity, split/merge candidates, historical-state
availability, and venue/closure correctness.

Generating the packet is not validation. Definitions remain `candidate` and
`manual_validation_status=not_reviewed` until adjudications are imported by a
future explicit process. Precision/recall must not be reported without sufficient
adjudicated strata.

Method B has a separate deterministic blinded packet and unblinding key under
`output/manual_review/revision_diff/`. Candidate order is hash-randomized and the
reviewer packet omits automated safe/promote decisions; primary target/predecessor
excerpts, exact candidates, ranges, lifecycle, signatures, actors, offsets, and
assignment evidence are included. See `REVISION_DIFF_RECOVERY.md`.
