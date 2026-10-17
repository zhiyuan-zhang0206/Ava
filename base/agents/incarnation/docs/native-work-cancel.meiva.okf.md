---
type: doc
title: Guarded native work cancellation
description: Exact acceptance and original cancellation, with interrupted work held for inspection.
---

# Guarded native work cancellation

The immutable `native_graph_work.id` names one actual invocation. Its tuple
includes agent, home machine, generation, host owner and protocol 1.
`agents_meta.native_work_id` gates single-flight admission. Pending strong
commands prevent a replacement from treating unknown execution as stopped.

`base/agents/incarnation/native_work.py` owns admission; the dedicated cancel
command owner is `base/agents/inbox/native_cancel.py`. HTTP observation and
acceptance remain exact-target, validated and idempotent; SDK transport retries
reuse the same key and replay the original command. Acceptance is a receipt for
the request, not a promise that execution stopped. See
[[gateway/agents/docs/control-acceptance.meiva.okf.md]].

The live original invocation observes cancellation, unwinds its known resources,
records the halt marker and flushes its checkpoint. Only this original path can
produce a new APPLIED receipt. An interrupted or uncertain command remains held
for inspection. A successor cannot ACK another owner's work, reconstruct an ACK
or write a recovery pause; only the exact original owner settles it.

Database-only phases have a finite deadline. A graph is invoked once; failures
in execution-result, flush or command writes leave the current operation failed.
No database-recovery loop re-enters it. Unknown execution results are reported as
`unknown`, never as proof that code did not run.

Host cancellation sends cancellation to the owned task and bounds cleanup.
Timeout retains unknown resource evidence and requires host shutdown. Known
child cleanup, identity checks, unique ownership and unknown-stop admission gates
remain; complete closure of every escaped descendant is not promised.

A fresh wake starts at claim with `halted=True`. Only new inbound or explicit
control can resume work. Claimed interrupted chat is consumed without automatic
redelivery. A new message may request new work, but old pending execution is not
replayed. Strong controls with unknown outcomes can still block that new work.

Older binaries do not acquire these guarantees from database schema alone.
Retirement and deployment remain operator actions. SDK work control is described
in [[meiva/agents/docs/work-control.meiva.okf.md]]; resource admission remains owned by
[[incarnation-resources.meiva.okf.md]].
