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
