# Pause, stop, and resume a cluster

Use the `ava` belonging to each unit's checkout. `ava pause`, `ava stop`, and
`ava start` act on **one local home**; they do not stop every machine remotely.
For a planned cluster outage, coordinate the participating machines through
SSH or their existing operator entry points. Stop runners before the gateway;
start the gateway and verify its dependencies before starting runners.

## Choose the resource scope

| Command | Native agent execution | Persistent shells and schedules | Local infrastructure and extras |
| --- | --- | --- | --- |
| `ava pause` | Drain normally and stop the native services | Retained | Keep PostgreSQL, Redis, PgBouncer, browser, Gate, helper and native LGTM |
| `ava stop -y` | Same normal drain | Close terminal jobs and shells | Stop this home's services, browser, Gate, helper, native LGTM and private data plane |
| `ava stop -y --keep-infra` | Same normal drain | Close | Keep the private data plane |
| `ava stop -y --keep-infra --keep-service gateway` | Same normal drain | Close | Also retain the named service; dependent services require `--keep-infra` |
| `ava start` | Restore normal admission after readiness | Reuse retained sessions; closed sessions are not serialized | Bring up enabled services from the existing home |

`--keep-service` is repeatable and accepts the bare service-roster name. It is
an invocation's preservation choice, not a permanent disabled-service setting.
`stop` asks for confirmation unless `-y` is passed; `pause` does not. Both use
`--timeout 300` by default. A deadline is a failed stop, not permission to kill
survivors. `--force` explicitly selects force behavior when normal exit cannot
complete. Force stops the selected service processes without fabricating a
restart receipt. Later start uses agent-host crash recovery from persisted
checkpoints.
Force does not provide normal pause's seamless continuation or a checkpoint
for interrupted arbitrary code; use normal stop for the planned data-plane move.

A stop retains agent IDs, history, checkpoints, pending messages, workspaces,
browser profiles and observability data. It does not terminate agent identities
or destroy the cluster. A full stop closes persistent shells; their running
processes and shell variables cannot be restored by `start`. Use pause when
those live sessions must survive. Windows' user-wide permissions helper and
externally launched tools are not owned by one local home.

Impersonation is a separate agent identity protocol. These commands do not
request, acquire, renew or release external-agent control leases. Also, the
legacy `ava cluster pause NAME` is machine membership administration, not this
local maintenance operation; do not substitute it for `ava pause`.

## What normal drain waits for

The command holds new native admission, enqueues an ordinary `restart`, and
lets native claim consume it. The graph returns normally, its checkpoint is
flushed, and its original continuation and resources finish. A restart
arriving just after claim can allow another iteration before the next claim;
this is not an instruction-level freeze. SDK dependencies stay available
through this drain. Service stopping then closes new ops work and waits for
already-admitted handlers and executor work before signalling services. A
signalled gateway drains its in-flight connections under a finite budget
(`gateway.gateway_graceful_shutdown_timeout_seconds`, default 30s — well inside
this command's default 300s deadline); past the budget uvicorn cancels the
remaining request and stream tasks and the lifespan cleanup runs, so an
unfinished streaming response cannot hold the process open. The TTL reaper's
serial remote-dispatch batches stop starting new dispatches once shutdown
begins, so that cleanup waits for an in-flight dispatch, never the remaining
batch — deferred rows are re-selected by the next boot's pass.

The existing home-local journal survives a CLI crash, host reboot and an
offline database. An incomplete drain or stop retains the hold and reports
failure. A drain that hits its deadline reports every unfinished agent — its
restart command's delivery state, the row's owner/lease/resource facts, the
agent's last activity and the live host's view — and names the predecessor-owner
fence explicitly when a successor boot is looking at a row its predecessor left:
that one needs `ava maintenance status` plus an explicit `resume --cancel` or
`repair`, never a retry loop. A rollout's own pause no longer defers the held
continuation it requires, so the ordinary update drain consumes and certifies.
Retry the command, or run `ava start` to restore services and release
the hold after readiness succeeds. A failed start keeps admission closed.
A recorded checkpoint/continuation failure blocks ordinary start and resume
before services are launched; repair and inspect that failure first. A healthy
service probe cannot prove that a failed checkpoint became durable.
`GET /api/health` and ops `status_probe` remain available during this hold, so
start can measure real readiness before opening business requests or native
admission. Public health still verifies identity and database access; status
and resume retain their existing authentication requirements.
The existing exact-generation `cluster_resume` RPC is reachable but refuses
resume before readiness or after a recorded continuation failure. Neither
`--no-readiness-gate` nor a waived update exit code certifies readiness.
Normal commands manage their own operation identity; there is no operation ID
or timestamp to copy between machines.

Once stop has completed without recorded failures, repeating `ava stop` can
read the existing local journal and finish without fetching an offline
gateway. An incomplete, corrupt or failed journal does not enable this
exception; the first normal drain needs the actual cluster configuration.

Cold native admission consumes the saved lifecycle pointers and checkpoints.
Idle agents remain idle; pending work can continue; terminated agents remain
terminated. Successful drain preserves completed tool results. No lifecycle
command can guarantee exactly-once external effects if a process crashes after
the external effect but before its result becomes durable.

## Updating and moving the data plane

`ava cluster update` uses the same native drain and retains persistent PTYs.
Schedules already running in those terminals continue with their loaded code;
new schedule-runner code needs an explicit schedule restart at an appropriate
work boundary, or a later full stop/start. Updating a schedule template on disk
does not rewrite its authoritative database script.

For a move, stop all participating runners, then stop the gateway last. Check
each command's exit status before taking the final snapshots. PostgreSQL uses
smart shutdown; open clients can prevent completion. Redis uses `SHUTDOWN SAVE`
and its exact process exit is verified. Stop is not a backup: validate the
separate database/Redis snapshots and required files before restoring them on
the destination. Keep every unit's home-local pause journal with that home.

Home-owned native LGTM stops after its producers. Its marker, unit definitions
and data remain intact for start. Linux user units disable the supervisor's
automatic SIGKILL; a timeout remains an incomplete stop. On macOS, the stop
first waits for the observed process generation and its descendants to exit,
then removes an idle launchd job; a KeepAlive replacement observed in between
is drained too. The final launchd inspection and removal are not an atomic
admission fence. Unregistered detached jobs and OS watchdog/autostart or daily
log-maintenance producers outside the service roster need their own scope check for a coordinated machine
shutdown; do not equate local command success with every remote writer stopping.

Every running daemon must already support the drain protocol. The command
checks the actual hosted daemon's home, PID, protocol and boot owner. Updating
source on disk does not update an imported running process. **The first
deployment is not protected by the new protocol itself**: establish and verify
its bootstrap procedure against the old running version before upgrading it.

Cold preparation can retain an expired owned idle row only when its native
consumers are absent, resources are empty and the latest persisted checkpoint
is a complete halted END. The same boundary can park a completed legacy
restart stranded in `restarting`: its done, untargeted command must precede
the final exit checkpoint. Only the parked status changes; historical leases,
identity, messages, checkpoints and lifecycle acknowledgements are preserved.
An expired lease alone, unfinished lifecycle/graph work or an uncertain
checkpoint still refuses; queued ordinary messages remain available for resume.
An unfinished agent lifecycle command (a competing restart/terminate) is
bounded-waited before refusing — preparation retries under the same row locks
until it resolves, then proceeds or aborts (task #3591); maintenance-authored
commands still refuse immediately.

## Explicit maintenance steps

`ava maintenance prepare/drain/status/stop/stop-data-plane/start/resume` remains
available for an operator who needs to inspect intermediate phases. These are
local commands using the same journal and native drain, not a second agent
ownership mechanism. They take a matching `--operation` and timezone-aware
`--acquired-at`; ordinary pause/stop/start does not need these arguments.

The explicit `maintenance stop` retains the data plane and refuses live
terminals unless `--keep-terminals` asserts a separately verified work boundary.
On a gateway, `--gateway-last` asserts the remote stops were independently
verified. `maintenance stop-data-plane` separately stops the verified private
data plane and saves Redis. `maintenance start` keeps admission held for an
explicit `maintenance resume`; ordinary `ava start` can instead complete the
same recovery and resume after its readiness gate. `resume --cancel` is for
abandoning preparation/drain while services are usable, not for bypassing a
partial stop or a failed startup.

A drain aborted by failed receipts keeps the hold, and `resume --cancel`
refuses while blocked failures remain. Receipts whose turn raised a
database-outage exception (`psycopg.OperationalError`, `PoolTimeout` — the
crash-equivalent family; every database channel hang surfaces as one of
these) do not block: they are recorded
as undelivered, and after the channel recovers the host re-drives the
held-control path (explicit re-flush, then restart claim) before the drain can
certify. For genuinely blocking failures, fix the root cause first, then run
the sanctioned repair:

```
ava maintenance repair --operation <operation> --acquired-at <timestamp> [--operator "Ava #1234"]
```

Repair requires the exact generation capability, refuses while the agent-host
still has active continuations, and records operator identity (timestamp,
operator label, OS user/uid/pid, parent process, machine) in the journal —
both sides of the repair CAS stay visible via `ava maintenance status`. A
partial release after a successful repair is completed by `resume --cancel`.

## Recovering a stuck maintenance operation

A maintenance hold does not expire on its own, and an incomplete pause or
stop retains its journal — which survives a CLI crash, a host reboot and an
offline database — instead of unwinding. Since task #3270 a **pre-stop** hold
is no longer unconditionally hand-recovery: the operator-side entries stamp it
with the shepherding process, and a hold whose shepherd is gone, whose
failures are empty and which nothing is executing under is declared
`abandoned` at the 10-minute notice bound and released by the pause watchdog
after a 30-minute observation window — `resume --cancel`'s automatic twin,
loudly audited, disable with `AVA_ABANDONED_HOLD_AUTO_RELEASE=0`. Everything
else stays loud and manual: failed receipts, a started stop, legacy journals
without a recorded shepherd, unreadable probes, and a still-live ladder. When
such a host is found mid-maintenance — services stopped or admission held, and
nothing left running that owns the pause — recover it by hand.

Read the phase first. Every explicit command takes the same `--operation` and
timezone-aware `--acquired-at` the hold carries, and `maintenance status`
prints both, the phase, and the recorded shepherd — the
binding process's pid/argv, its session leader, and the judged liveness
(`alive`/`dead`/`missing`/`unreadable`; null when no identity was recorded):

```
ava maintenance status
```

| Phase found | Steps back to service |
| --- | --- |
| `preparing`, `draining` | `ava maintenance resume --cancel` — abandon the drain while services are still usable. |
| `drained` | `ava maintenance resume --cancel` returns to service. To carry the planned stop through instead: `ava maintenance stop`, then `maintenance start`, then `maintenance resume`. |
| `stopping` | The stop died or timed out mid-way: re-run `ava maintenance stop` (it re-verifies the drain and finishes the service stop), then `maintenance start`, then `maintenance resume`. |
| `stopped` | `ava maintenance start` — the ordinary bring-up, with admission kept held — then `ava maintenance resume` to release the hold after readiness. |
| `starting` | A bring-up died mid-way: re-run `ava maintenance start`, then `maintenance resume`. |

On a gateway, `maintenance stop` requires `--gateway-last` (the operator has
independently verified every remote stop), and it refuses live terminals
unless `--keep-terminals` asserts a separately verified work boundary. An
ordinary `ava start` can also complete the stopped/starting recovery end to
end: it restores service and resumes after its readiness gate, without the
explicit hold — unless blocking failed receipts remain, in which case it
refuses before launching services (clear those first, as for `resume --cancel`).

`resume --cancel` refuses while blocking failed receipts remain. Fix the root
cause first, then release the latch with the sanctioned repair — only on a
`preparing`/`draining` hold, and only while the agent-host has no active
continuations:

```
ava maintenance repair --operation <operation> --acquired-at <timestamp> [--operator "Ava #1234"]
```

The repair moves the failed receipts to `repaired` (both sides stay visible in
`maintenance status`), records operator identity in the journal, and releases
the hold in the same command; if that release is interrupted (a partial
release), `resume --cancel` completes it.

## Stop-class drills and operations: executor-cancellation insurance and hold handover

A stop-class drill or operation — any leg that takes the message plane down with
it — must survive its executor's cancellation. The 2026-09-17 S3 incident is the
proof case: the executor's turn was cancelled after the stop leg, the start leg
never ran, and the retained non-expiring hold left the whole platform dark for
~110 minutes (#3719). At the cancellation-plus-10-minute mark the entire
recovery was a single official `ava start`.

1. **Name a rescue actor outside the blast radius.** Before the first stop leg,
   designate one rescue actor plus a backup on a machine that stays up, and one
   out-of-band signal (the OOB probe). The rescue actor observes directly —
   read-only over an independent link (ssh plus state files), never through the
   message plane, which is itself the surface under test — and never executes
   stop legs.

2. **Keep the progress record off the stopped surface.** The authoritative
   record is a progress file written on a non-target machine (or pulled there
   every step); the task registry is a sync target only. On 2026-09-17 both the
   operation log and the message plane died with the stack (log frozen 08:34:55;
   the executor's send failed 08:50:39). Any monitoring or automation for the
   operation lives on a surviving OS surface (launchd / cron / scheduled task),
   never inside an agent turn.

3. **Stage the exact rescue ladder for every phase, with a force tier.** Use the
   phase table above; the staged set is (a) an incomplete stop → a bounded re-run
   of the stop, then the documented `stop --force` escalation when a graceful
   leg provably cannot complete (2026-09-17: two normal attempts timed out;
   force finished in 12s), then `maintenance start` / `maintenance resume`;
   (b) stopped → `ava start` end-to-end (or `maintenance start` plus
   `maintenance resume` per the table); (c) starting → re-run
   `maintenance start`, then `resume`. Verify the read-only parts in advance,
   ssh access and `sudo -n` included. The rescue actor is pre-authorized to use
   `--force` once the abort bound has been declared — a fresh approval round
   would burn the window — but never against a competing rollout.

4. **Heartbeat and the abort bound.** The executor records a progress line
   every step and at least every 5 minutes. No progress for 10 minutes is the
   abort bound for the operation — judged on observable evidence (return code
   not delivered, state-file timestamps not advancing, surviving process
   inventory unchanged). It shares its numeral with the abandoned-hold notice
   bound but is a different instrument: that bound judges an idle hold for
   release, while this one bounds executor silence during a live operation. The
   rescue actor then executes the staged recovery, announces it, and records the
   handover (who, when) in the operation record.

5. **Bound every hold and name its rescuer.** This section extends the pre-stop
   abandoned path of the table above: a started stop is never auto-released; for
   that class the named rescuer and the stated maximum intended lifetime are the
   insurance. A drill hold carries both, written alongside the hold, together
   with the window end and a reference to the staged commands. A hold with a
   dead shepherd, empty failures, nothing executing under it, and an age past
   its bound is an orphan: escalate through the concrete available mechanisms —
   the pause watchdog, the stranded-hold controller, the machine-local alarm
   path — always out-of-band, then recover via the official path. A release
   before the declared lifetime, or without the rescue actor's handover record,
   is an anomaly to surface to the operation owner. During a stop-class window,
   an external party may judge locks stale and clear them (user-side Codex does
   this legitimately): announce at window start that `drill-*` locks must not be
   cleared while the window is open, watch for releases, and treat an in-window
   external release as stolen — abort that step's reading, carry the completion
   chain through, and take evidence after the window. To intentionally keep a
   host down, keep the ladder's session alive — the live shepherd is the intent
   marker — or, until the orphan-completion design names a dedicated marker,
   pin the existing `AVA_ABANDONED_HOLD_AUTO_RELEASE=0` (gateway/cluster
   setting).

6. **Carry the operation across turns.** Run stop-class operations as a task
   backed by the surviving-machine record described above — never as an
   unlogged one-shot — so a successor continues from the record plus the staged
   commands instead of restarting.
