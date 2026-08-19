# Manual review instructions

`wikidisputes-ssot review-packet` samples deterministic strata using seed 20260818
and writes `output/manual_review/ssot_review_packet.parquet`. It includes DV state,
reply resolution, signature/link evidence and all cross-label episodes when those
tables are available. Population counts and sampled IDs are in
`output/reports/manual_review_packet.json`.

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
