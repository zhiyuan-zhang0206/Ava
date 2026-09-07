---
type: doc
title: Hosted Runtime Admission
description: Database ownership fences hosted turns and lifecycle controls.
tags: []
---

# Hosted Runtime Admission

`agent/hosted_ownership.py:admit_hosted_runtime()` locks the agent metadata
before granting execution to the local agent host. The row carries generation,
owner and `runtime_kind='hosted'`; the context carries that admitted identity
through the entire turn. A fresh foreign owner, terminal status, changed home or
maintenance hold cannot be bypassed by a delayed wake.

Ownership survives normal idle and runtime-cache eviction. The daemon renews
only rows belonging to its own boot owner. Settlement and release match the
original incarnation, so a late turn cannot rewrite a replacement's state.

Restart and terminate are durable inbounds. Native claim returns to END, the
host flushes the final checkpoint, then applies the accepted lifecycle command.
Single-flight remains held through the original continuation and settlement.
Restart releases the incarnation for the next admission; terminate leaves the
agent identity terminal. There is no child-process admission, OS launch budget,
canonical per-agent process record or `/exited` callback.

Force control retains its separate exact-incarnation resource fence. Acceptance
means the interruption was enqueued; it does not certify that an uncooperative
turn or disposable execution domain has finished.

Historical database rows and command payloads are retained. Runtime removal
requires no destructive schema migration.

Related: [[../lease.ava.okf.md]], [[../lifecycle.ava.okf.md]], and
[[shared/maintenance.ava.okf.md]].
