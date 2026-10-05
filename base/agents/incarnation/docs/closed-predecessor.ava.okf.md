---
type: doc
title: Closed predecessor
description: Retired-shape resource values, the closed-predecessor form, drained resource sets, never-admitted resurrection and minted identities of legacy terminated rows.
tags:
- base
- lifecycle
---

# Closed predecessor

A stored value the current model cannot decode (a retired writer's process
receipts without boot scope, or malformed bytes) raises `ResourceShapeError`.
No runtime path parses or converts it. Hosted admission records
`resource_fence` and logs the cause at WARNING, local recovery skips the
value, and resurrection refuses with `runtime_cutover_required`.

The closed-predecessor form is the incarnation a retired value named, with no
host identity, no freeze and an empty set; rows converted from the retired
shape carry it. It is backed by that incarnation's existing predecessor
receipt (the old drain's applied restart still held as the lifecycle pointer,
or an applied and observed terminate). The ordinary predecessor rule admits
the form once: admission rewrites the set for its own incarnation and
observes a restart receipt. Without its receipt the form is not admissible,
and a same-owner continuation refuses it because no host identity is
recorded.

A drained restart leaves NULL or the complete, empty, unfrozen set of exactly
the incarnation it released (`DRAINED_RESOURCES`). The host drain receipt,
preparation retry and drain certification accept both; any other set is not
drained.

A terminated row that was never admitted resurrects as a fresh hosted birth
only when that is proven: no runtime identity and the fresh-INSERT birth
marker unconsumed, rechecked in the final CAS. NULL resources are unknown and
refuse there. A row the current runtime force-terminated while it was
unowned resurrects the same way. It carries no resource evidence; the force
recorded an `unowned_termination` receipt because the runtime's own birth
epoch or `lifecycle_release` had left the row unowned
(`docs/decisions/2026-09-29-unowned-termination-resurrects.md`). A legacy row
never qualifies.

An agent terminated before the runtime incarnation existed has NULL resources
and an incomplete identity, so resurrection refuses it too, unless the row
carries a minted hosted identity: kind `hosted`, a fresh generation and owner,
no pid. Resources stay NULL and no receipt exists. That identity only passes
the resurrection gate; the resurrection CAS clears it, and the successor is
admitted as protocol zero like any NULL row. It is never admission evidence
(`docs/decisions/2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md`).
No code mints such an identity.

Why: `docs/decisions/2026-09-27-existing-agent-closed-predecessor-admission.md`.
