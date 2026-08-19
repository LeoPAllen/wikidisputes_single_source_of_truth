# Chronology, lifecycle, and reply repair

Logical chronology uses recovered creation/addition time, numeric creation revision,
numeric position components, original row order and logical UID. A modification
timestamp never replaces creation time. Equal creation times share a stable
simultaneity group; deterministic tie-breaking is not evidence of causal order.

Lifecycle actions remain distinct versions of one logical utterance. Source and
WikiConv observations can both evidence that lifecycle. Unresolved source-only
actions remain explicit rather than being fabricated as known creations.

Reply repair resolves raw targets through current, original, ancestor and parent
aliases. Every edge retains its raw target, selected logical target, method/status,
reason, confidence, child-before-parent/equal-time flags, lag and indentation.
Ambiguous/unresolved targets are not discarded. Self-reference and lifecycle/reply
cycles are quality flags or quarantined branches.

Human display order is separate from utterance order: headings occur first as
`row_kind=context`, remain joinable, and are not annotatable. Article edits only
occur in the event timeline.
