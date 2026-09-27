---
type: doc
title: Closed predecessor
description: Retired-shape resource values, their one-time cutover conversion, drained resource sets and never-admitted resurrection.
tags:
- shared
- lifecycle
- cutover
---

# Closed predecessor

A stored value the current model cannot decode (a retired writer's process
receipts without boot scope, or malformed bytes) raises `ResourceShapeError`.
No runtime path parses it. Hosted admission records `resource_fence` and logs
the cause at WARNING, local recovery skips the value, and resurrection refuses
with `runtime_cutover_required`.

Only the one-time cutover replaces such a value, per row
(`shared/predecessor_closure.py`, called by `scripts/cutover_db_records.py`
and deleted with it). The closed-predecessor form is the incarnation the
retired value names, with no host identity, no freeze and an empty set. It is
backed by that incarnation's existing predecessor receipt (the old drain's
applied restart still held as the lifecycle pointer, or an applied and
observed terminate) and by the machine's closure attestation (its sha256; the
document itself is kept in the cutover record), stored with the before image
and the operator on the receipt. The conversion compares the exact before
image, and refuses NULL, current-model values, a live or different runtime on
the row, an unsettled command, another machine, or a receipt that already
carries a closure. The ordinary predecessor rule then admits the form once: admission
rewrites the set for its own incarnation and observes a restart receipt. A
same-owner continuation refuses it because no host identity is recorded.

A drained restart leaves NULL or the complete, empty, unfrozen set of exactly
the incarnation it released (`DRAINED_RESOURCES`). The host drain receipt,
preparation retry and drain certification accept both; any other set is not
drained.

A terminated row that was never admitted resurrects as a fresh hosted birth
only when that is proven: no runtime identity and the fresh-INSERT birth
marker unconsumed, rechecked in the final CAS. NULL resources are unknown and
still refuse.

Why: `decisions/2026-09-27-existing-agent-closed-predecessor-admission.md`.
