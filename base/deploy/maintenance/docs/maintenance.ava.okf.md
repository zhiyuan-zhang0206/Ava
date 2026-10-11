---
type: doc
title: Native pause and maintenance
description: A durable home-local admission hold around existing restart, claim and checkpoint boundaries, shared by stop, restart and update.
status: current
---

# Native pause and maintenance

`admission.py`, `state.py` and `cohort.py` extend the
[pause-owner journal](pause_owner.ava.okf.md). There is no new database
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
Unowned idle rows with NULL resources or an unconsumed birth marker are
parked when lifecycle work is settled. Stale owners and ambiguous work refuse drain.

Already-stopped hosted units also preserve legacy idle rows whose lease is
NULL and whose PID/resources are empty, after proving the local host absent.
An applied old restart and claimed ordinary work remain untouched for normal
cold admission. `cold.py` also recognizes retired owned idle rows
with an expired lease. That requires an absent native host/legacy consumer,
empty PID/resources, no live or unattributable exec request evidence (a provably
stale envelope is quarantined with a receipt, not deleted), and the latest
persisted v4 checkpoint at END (halted, no ready channels or pending
tasks/writes). Preparation holds the metadata row lock and rechecks the latest
checkpoint identity; it writes nothing, so historical leases, owners/generations,
commands and checkpoints stay unchanged.
Unknown checkpoint versions, live consumers and fresh leases still refuse.
This is preserved idle intent, not a fabricated continuation receipt.

Hosted drain receipts require the shielded continuation, final checkpoint
flush, resources and owner settlement to finish. The matching DB restart must
be applied and unobserved. A successor cannot sign an absent original receipt.
Failures latch before journal I/O. When the journal is read successfully and
no hold exists, an ordinary failure does not fence a future maintenance generation;
unreadable state and failures during the current hold remain fenced. Held
lifecycle controls flush any prior buffered checkpoint before claiming their
restart, without invoking the graph or consuming ordinary messages;
`applied_at`, an idle row, or a released lease alone is insufficient.

Continuation failures, including database connection failures, are recorded
as blocking maintenance failures. Same-boot and persisted hold fences prevent
automatic re-drive after failure. Explicit operator resume is required; restart
does not turn an unknown continuation into a successful drain receipt.

Phases are `preparing → draining → drained → stopping → stopped`, owned by
`state.MaintenancePhase`; `ava stop` / `ava restart` walk all five. Journal
reads validate the enum. There is no start phase: `ava start` authorizes bring-up
against the retained hold and releases it after actual readiness. Nested calls
pass the authorized operation snapshot and verify its holder and acquisition time.

A recorded blocking continuation/flush failure prevents drain, phase transitions
and bare resume. Explicit `ava start` may settle the failed receipt after the unit
serves, preserving a pending/claimed restart pointer or reporting a lost pointer
before clearing the receipt by compare-and-swap. A new failure keeps the hold.
This operator action does not replay the original graph or code execution. Pending
restart controls remain subject to resource and lifecycle gates; unknown compact
or cancel attempts stay held. New ordinary execution requires fresh input.

Repeating a completed, failure-free stop reads the journal before configuration
bootstrap. First-time normal drain still needs data-plane configuration.


SDK dependencies remain available through prepare/drain. Service stop closes
new ordinary ops admission and waits for admitted handlers and executor work before
signalling services. `ava restart` retains infrastructure and persistent PTYs;
`ava stop` closes terminal jobs and shells and stops home-owned infrastructure
unless explicitly preserved. `service_stop` verifies process identities
and exits; `data_plane/maintenance_stop` saves Redis before its verified shutdown.
`_stop_extras` covers home-owned Gate/helper/native LGTM outside the session
roster, retaining desired configuration and data. None of these local checks
proves that every remote or unregistered writer has stopped.

During a stopped hold, gateway `GET /api/health` remains a control-plane
identity and real database probe. It remains public and reports database
failure as degraded; it does not certify business admission. Ops `status_probe`
also stays reachable and remains counted through completion. Its existing
authentication requirements are unchanged.
Resume still checks the exact generation, failure receipts and actual serving
state. Ordinary start proves readiness before releasing the hold; an exit-code
waiver never certifies serving. Business APIs and native work stay closed until
that successful resume.

Explicit force kills native processes without stamping a normal termination or
inventing a restart/flush receipt. It preserves original metadata for inspection;
crash reaping reports termination without resurrecting or replaying old work.

`ava status` reads the hold journal (`ava status --json` prints only the hold) and
`ava start` is its only exit, over a hold in any phase. It cannot prove replay
safety for a failed arbitrary external effect. A stop that failed past the drain
is retried with `ava stop`, or finished with `ava start`.

See [operator procedure](../../../../docs/conventions/operations/graceful-maintenance.md) for
resource scopes, recovery and the first-deployment limitation.
