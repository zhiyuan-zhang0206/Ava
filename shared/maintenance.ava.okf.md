---
type: doc
title: Native pause and maintenance
description: A durable home-local admission hold around existing restart, claim and checkpoint boundaries, shared by pause, stop and update.
status: current
---

# Native pause and maintenance

`maintenance.py`, `maintenance_state.py` and `maintenance_cohort.py` extend the
existing [pause-owner journal](pause_owner.ava.okf.md). There is no new database
pause table or agent graph hook. The exact `(holder, acquired_at)` operation is
stored in `$AVA_HOME/run/deploy-pause-owner.json`; it has no TTL. Invalid or
unreadable state refuses admission. External-agent identity leases are a
separate protocol and are not acquired or released by native maintenance.

`ops.agent_pause` publishes the hold before locking native `agents_meta` rows.
Hosted admission checks it at the row-lock boundary. Existing
iterations run until an ordinary `restart` reaches claim. Lifecycle priority
preserves pending ordinary messages. A restart arriving after claim can allow
one more iteration; this is not an instruction-level freeze.

The journal records one restart ID per captured live incarnation. Preparation
first records the cohort, commits commands, then records their IDs. A retry
reuses the same commands. Normal claim binds the target generation and owner.
Clean unowned idle rows without resources or unresolved lifecycle work are
parked without being relaunched. Stale owners and ambiguous work refuse drain.

Already-stopped hosted units also preserve legacy idle rows whose lease is
NULL and whose PID/resources are empty, after proving the local host absent.
An applied old restart and claimed ordinary work remain untouched for normal
cold admission; an expired lease or an unapplied control does not qualify.
This is preserved idle intent, not a fabricated continuation receipt.

Hosted drain receipts require the shielded continuation, final checkpoint
flush, resources and owner settlement to finish. The matching DB restart must
be applied and unobserved. A successor cannot sign an absent original receipt.
Failures latch before journal I/O;
`applied_at`, an idle row, or a released lease alone is insufficient.

Phases are `preparing → draining → drained → stopping → stopped → starting →
ready`. Failed prepare/drain/stop/start keeps the hold. Ordinary `ava start`
authorizes the existing operation for bring-up and resumes after readiness;
explicit `ava maintenance start` leaves resume to the operator. The internal
`authorized_start` ContextVar is exact-operation authority for nested calls,
not a service-process credential. Stranded-pause recovery cannot abandon a
maintenance hold. A recorded continuation/flush failure blocks ordinary start
and every resume path before admission is released. Resume wakes the saved restart IDs; DB pointers survive a
lost Redis wake. Cold admission reloads checkpoints, leaves idle agents idle,
continues unfinished work and does not revive terminated identities.
Repeating `stop` after a completed, failure-free stop reads this same journal
before configuration bootstrap, so an offline gateway does not prevent the
idempotent stop. First-time normal drain still needs data-plane configuration.

SDK dependencies remain available through prepare/drain. Service stop closes
new ops admission and waits for admitted handlers and executor work before
signalling services. `ava pause` retains infrastructure and persistent PTYs;
`ava stop` closes terminal jobs and shells and stops home-owned infrastructure
unless explicitly preserved. `_maintenance_stop` verifies process identities
and exits; `_maintenance_data_plane` saves Redis before its verified shutdown.
`_stop_extras` covers home-owned Gate/helper/native LGTM outside the session
roster, retaining desired configuration and data. None of these local checks
proves that every remote or unregistered writer has stopped.

Explicit force kills native processes without stamping a normal termination or
inventing a restart/flush receipt. It preserves the original metadata for the
existing crash-recovery policy, whose auto-resurrection and retry limits still
apply; it does not promise the normal drain's continuation guarantee.

The compatibility `ava maintenance` surface exposes intermediate phases.
Its service/data-plane stop refuses live terminals unless `--keep-terminals`
asserts a separately verified work boundary. This flag preserves the terminal,
not proof that its script cannot write. `resume --cancel` restores ordinary
recovery during preparation/drain; it cannot bypass a partial service stop or
prove replay safety for a failed arbitrary external effect.

See the [operator procedure](../conventions/graceful-maintenance.md) for
resource scopes, recovery and the first-deployment limitation.
