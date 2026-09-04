---
type: doc
title: Updater spawn handoff
description: Host-local process ownership across the pause-to-detached-updater lifetime.
status: current
---

# Updater spawn handoff

`$AVA_HOME/run/updater-handoff.json` is a host-local ownership fact, not a UI
marker. Under the short lifecycle mutex, `spawn_update` publishes a `pending`
generation before pausing and starting the detached updater. A definitive
backend decline CAS-clears that generation and compensating-unpauses; an
ambiguous post-fork/Popen failure preserves it because the child may exist.

The child must acquire the existing updater OS mutex and CAS the exact fresh
`pending` generation to `running(pid, create_time)` before any checkout, DB
lease touch, stop, or restart. A losing or late child exits without mutation.
The running generation remains for the child’s entire lifetime and is
CAS-cleared in its terminal/finally path. Postgres updater-lease writes remain
fail-soft observability and are never the ownership transfer.

Pending expiry only permits a recovery proof; it is not proof that a child is
dead. A running marker never expires by age. Manual and automatic recovery
treat PID reuse or `NoSuchProcess` as death evidence, while access/read errors
fail closed as live. Recovery also checks session and DB-lease evidence under
the lifecycle mutex before unpausing and clearing the captured generation.
Writes are atomic, mode `0600`, and generation-CAS.

The updater OS mutex keeps a permanent stable inode: release unlocks and closes
without unlinking, and only genuine contention errors become a `False` result.

The restricted immutable-ops updater leg retains its compensating inputs in a
separate `$AVA_HOME/run/updater-bootstrap-recovery.json` envelope. Version 1
binds the handoff generation, private request, context and complete inventory
receipt hashes, the exact known cron definition, transition stage, and at most
64 phase observations within a 256 KiB file budget. No database URL or secret
is recorded. `shared/updater_recovery.py` owns the strict typed journal schemas;
partial terminal-looking dictionaries, a last phase that differs from the outer
stage, or nested normal evidence outside its planned candidate-ready bootstrap
are malformed evidence, not permission to clear. The separate envelope makes
malformed bootstrap evidence auditable without making an unrelated malformed
ordinary spawn marker permanently unrecoverable.

An unfinished or malformed bootstrap envelope cannot be cleared, replaced by
ordinary `begin`, or discarded by generic recovery. Only checked recovery under
the existing updater mutex can reclaim the same generation after exact owner
death. Initial bootstrap ownership is itself a CAS over the prepared predecessor
generation, PID, and birth time; a replacement marker cannot be overwritten.
A missing post-fork session record remains ambiguous, not permission to launch
another process. Verified-A recovery or terminal restricted-B readback without
a normal continuation allows the generation-CAS clear of both files.

An admitted normal continuation nests its phase journal inside the same
versioned bootstrap envelope only after exact `candidate_ready` evidence and
same-process handoff ownership. The bootstrap writer records
`normal_release_planned` before the first normal-journal write, so even failure
of that first write remains non-clearable. Once nested evidence exists,
bootstrap writes cannot discard it; normal writes preserve request, operation,
unit and selector identity and follow legal monotonic phases. Bootstrap phase
writes also preserve their bound request/digests/plan/cron and append exactly one
phase observation. This preserves the
bootstrap proof while keeping ordinary spawn ownership separate. Any planned,
unfinished or malformed normal journal blocks generic clear, replacement and
force-clear; only a fully validated `committed` normal journal permits the
generation-CAS clear of both files. Manual and stranded-pause recovery consult
this exact retained-envelope verdict before unpausing. Retained evidence is not
automatic rollback permission.
