# Graceful maintenance for a coordinated cluster stop

Use `ava maintenance` on each unit's own checkout. These commands act on **one
local home**. The operator coordinates all participating machines using an
existing transport such as SSH; this CLI does not discover SSH aliases or wake
stopped remote ops servers. Ordinary `ava stop` and the existing update policy
retain their own semantics and are not substitutes for this procedure.

The implementation uses the existing lifecycle path: enqueue `restart`, let
native claim consume it, return normally, flush the checkpoint, then close the
original continuation/resources. It does not stop arbitrary Python at an
intermediate instruction. A restart arriving just after claim may allow one
more iteration before the next claim. A timeout or failure is a refusal to
continue the shutdown, with no force fallback.

## Before the first use

Every running hosted daemon must already support the maintenance protocol.
`prepare` checks the actual daemon's home, PID, protocol and boot owner. Updating
source on disk does not upgrade a running process. **The first deployment of
this feature is not protected by the feature itself.** Older hosted runtimes do
not have its admission fence, and the old update Phase-A pause is not a global
checkpoint barrier. Establish and independently verify a safe bootstrap plan
before that deployment; this procedure does not supply a race-free shortcut.

The first implementation supports hosted agent runtimes. Resolve external
control leases before preparing. Unknown stale owners or process-mode native
runtimes are refused. Clean unowned idle intent is preserved without inventing a
restart or termination; this is not evidence that every legacy process is gone.

Persistent shells, schedules, watcher PTYs and their hosts need their own
completed-work boundary. By default, strict stop refuses live terminal resources;
it neither kills nor promises to serialize/replay them. When terminals must
remain open, `stop --keep-terminals` and `stop-data-plane --keep-terminals`
explicitly assert that the operator has separately verified their business
work has stopped. The commands preserve those terminals, including Windows
SDK shell and schedule records in the shared service namespace. The flag does
not inspect terminal activity, freeze input or prove that a PTY cannot write
to the old data plane. Stop active scripts and periodic writers at their own
safe boundary before taking the final snapshot; an idle shell may stay open.
Drain receipts, ops quiescence, exact service identities and normal exit checks
remain required. Independently managed
Gate, permission helper, Redis bridge, native LGTM, OS watchdog/autostart and
logs-maintenance jobs are outside the service-backend proof. Inventory their exact
home/PID identities and normal-stop behavior separately. Do not substitute a
broad OS kill command for proof of completed work.

## Stop and retain the recovery cohort

Choose one nonempty operation identifier and one timezone-aware timestamp and
use the exact same values on every participating home. For example:

```sh
ava maintenance prepare --operation gateway-move --acquired-at 2026-09-06T12:00:00Z
ava maintenance drain --operation gateway-move --acquired-at 2026-09-06T12:00:00Z --timeout 300
ava maintenance status
```

Preparation persists the original live hosted cohort and one ordinary owned
restart per agent. `drain` requires original-host receipts plus the matching
unobserved restart pointers, and an empty active-continuation set. The hold
survives host reboot and an offline database; normal startup and the 120-second
stranded-pause recovery cannot release it. Keep this journal with each unit's
home; preserve inbound rows and checkpoints together in the database move.

After **all participating units** have drained and independently managed work
has reached its own safe boundary, stop each runner's local recorded services:

```sh
ava maintenance stop --operation gateway-move --acquired-at 2026-09-06T12:00:00Z --timeout 300
```

Use `--gateway-last` on a gateway-capable unit only after independently verifying
the remote stops. This flag is an **operator assertion**, not an automatic
remote probe. Stop refuses new ops calls only at this stage; admitted request
handlers and executor futures must finish first. It signals exact captured
process identities normally and verifies those processes, their original POSIX
process groups and tracked descendants have exited. A newly created, unregistered
daemon that deliberately leaves the group needs separate ownership proof. Failed/ambiguous identity, a survivor or timeout retains the hold.
A success proves the declared local service set; it does not prove all OS-managed
extras, remote hosts or detached resources have stopped.

The local service stop leaves the data plane running. After the final database
and Redis snapshot has been created and validated separately, stop the gateway's
private native data plane:

```sh
ava maintenance stop-data-plane --operation gateway-move --acquired-at 2026-09-06T12:00:00Z --gateway-last --timeout 300
```

This validates home ownership and normal exits of PgBouncer, PostgreSQL and
Redis. PostgreSQL uses smart shutdown, so an open client blocks shutdown rather
than being aborted. Redis uses `SHUTDOWN NOSAVE`: **this command creates no
recoverable backup**. Remote-managed or unverified data planes are refused.
The claim is zero declared business writers, not zero PostgreSQL internal
background writes during a live snapshot.

## Start, then explicitly resume

Restore and verify the data/configuration through the reviewed migration plan.
Keep the original local journal and exact operation. Start the gateway/dependency
unit first, then the runners, through their existing OS/SSH entry points:

```sh
ava maintenance start --operation gateway-move --acquired-at 2026-09-06T12:00:00Z
ava maintenance status
```

A successful readiness result moves the journal to `ready`; a failed start stays
`starting` and can be retried. Agent admission and schedules remain held. Existing
disabled-service intent is preserved. After the operator verifies all required
dependencies and units, explicitly release each unit:

```sh
ava maintenance resume --operation gateway-move --acquired-at 2026-09-06T12:00:00Z
```

Native successors consume the retained restart pointers and reload their cold
checkpoints. Idle agents do not gain a synthetic model turn. Pending messages
remain pending and claimed messages follow existing checkpoint reconciliation.
Previously terminated agents stay terminated. Already-completed external effects
are not replayed by a successful drain; a crash before a durable result cannot
provide an exactly-once guarantee for arbitrary code.

If preparation or drain is abandoned while services/dependencies remain usable,
`resume --cancel` explicitly releases that same operation and returns its saved
cohort to ordinary lifecycle recovery. It never automatically abandons a hold.
Cancellation is allowed only in `preparing`, `draining` or `drained`, before
service stopping begins. It cannot bypass a partial stop or failed startup.
After a partial service stop, finish the verified stop and use maintenance start
before resuming. A failure during an external effect still requires inspecting
that effect; cancelling maintenance is not a claim that replay is safe.
