---
type: doc
title: Closed predecessor
description: Retired-shape resource values, their one-time cutover conversion, drained resource sets, never-admitted resurrection and the identity mint for legacy terminated rows.
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
carries a closure. It also refuses a row its successor would still refuse once
converted (`successor_refusal`, which the read-only survey applies too and
reports as `unconvertible`): a terminated row must still record exactly the
closed hosted incarnation with no lifecycle pointer, which is what resurrection
requires, and an idling row must have its owner released with at most the
drain's restart as its pointer, the only pointer admission observes. The
ordinary predecessor rule then admits the form once: admission rewrites the
set for its own incarnation and observes a restart receipt. A same-owner
continuation refuses it because no host identity is recorded.

A drained restart leaves NULL or the complete, empty, unfrozen set of exactly
the incarnation it released (`DRAINED_RESOURCES`). The host drain receipt,
preparation retry and drain certification accept both; any other set is not
drained.

A terminated row that was never admitted resurrects as a fresh hosted birth
only when that is proven: no runtime identity and the fresh-INSERT birth
marker unconsumed, rechecked in the final CAS. NULL resources are unknown and
refuse there.

An agent terminated before the runtime incarnation existed has NULL resources
and an incomplete identity, so resurrection refuses it too. The same cutover
script gives such a row, on a machine whose closure attestation it holds, a
minted hosted identity: kind `hosted`, a fresh generation and owner, no pid.
Resources stay NULL and no receipt is written. That identity only passes the
resurrection gate; the resurrection CAS clears it, and the successor is
admitted as protocol zero like any NULL row. It is never admission evidence
(`decisions/2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md`).
Its evidence is narrower than the conversion's: the row records no pid, so
the mint rests on the attested home's empty census and on the row's
termination no later than the attestation's `attested_at`. A machine with a
second unit left gets no mint, and the cutover's first run (W7) refuses while
a row reads as terminated after its attestation. A row whose lifecycle
pointer names a forced terminate converts at the W12 late conversion once the
new agent host settled that force at its first boot. Rows without an
attestation, on a machine with no unit or a paused one, with any other
pointer resurrection does not supersede, or terminated after the attestation
keep refusing.

Why: `decisions/2026-09-27-existing-agent-closed-predecessor-admission.md`.
