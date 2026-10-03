# Stop, restart, and resume a cluster

Use the `ava` belonging to each unit's checkout. `ava stop`, `ava restart`, and
`ava start` act on **one local home**; they do not stop every machine remotely.
For a planned cluster outage, coordinate the participating machines through
SSH or their existing operator entry points. Stop runners before the gateway;
start the gateway and verify its dependencies before starting runners.

## Choose the resource scope

| Command | Native agent execution | Persistent shells and schedules | Local infrastructure and extras |
| --- | --- | --- | --- |
| `ava stop -y` | Same normal drain | Close terminal jobs and shells (HUP/TERM, SIGKILL after a bounded grace) | Stop this home's services, browser, Gate, helper, native LGTM and private data plane |
| `ava stop -y --keep-infra` | Same normal drain | Close | Keep the private data plane |
| `ava stop -y --keep-infra --keep-service gateway` | Same normal drain | Close | Also retain the named service; dependent services require `--keep-infra` |
| `ava restart` | Same normal drain, then `ava start` | Retained | Replace the native services; keep PostgreSQL, Redis, PgBouncer, browser, Gate, helper and native LGTM |
| `ava start` | Restore normal admission after readiness | Reuse retained sessions; closed sessions are not serialized | Bring up enabled services from the existing home |

`--keep-service` is repeatable and accepts the bare service-roster name. It is
an invocation's preservation choice, not a permanent disabled-service setting.
`stop` asks for confirmation unless `-y` is passed; `restart` never asks. `stop`
uses `--timeout 300` by default, and restart's drain has the same budget. A
deadline is a failed stop, not permission to kill surviving services. Terminals and Postgres are the exceptions: Postgres' fast
shutdown that has not finished by the end of its share of the budget (a hung
archive command) is ended by an immediate shutdown and the leftover descendants
are SIGKILLed, loudly and without failing the stop
([decision](../decisions/2026-10-02-pg-stop-escalates-to-immediate.md)); for terminals, `stop` hangs up each shell's
whole session (its descendants and double-forked orphans included), and
SIGKILLs what is still alive after a grace of at most 10 seconds; a busy
session still leaves its owner the closure notice, written to the database
in the `terminals` phase, before the data plane stops
([decision](../decisions/2026-09-28-stop-escalates-to-sigkill.md),
[notice write](../decisions/2026-10-02-close-notices-written-at-terminals.md)). `--force`
explicitly selects force behavior when normal exit cannot complete. Force stops
the selected service processes without fabricating a restart receipt. Later
start uses agent-host crash recovery from persisted checkpoints.
Force does not provide normal stop's seamless continuation or a checkpoint
for interrupted arbitrary code; use normal stop for the planned data-plane move.

A stop retains agent IDs, history, checkpoints, pending messages, workspaces,
browser profiles and observability data. It does not terminate agent identities
or destroy the cluster. A full stop closes persistent shells; their running
processes and shell variables cannot be restored by `start`. No `ava stop` option
keeps them; only `ava restart` does.
Externally launched tools are not owned by one local home.

Impersonation is a separate agent identity protocol. These commands do not
request, acquire, renew or release external-agent control leases. Also, the
legacy `ava cluster pause NAME` is machine membership administration, not this
local maintenance operation; it stops nothing on this home.

## What normal drain waits for

The local maintenance journal is also the gateway's HTTP admission authority.
Draining or locally drained units keep dependency APIs available to other hosts.
The stop/start window (`stopping`, `stopped`, `starting`, `ready`) closes business
requests until the same journal is atomically resumed. Completion has no posture
cache expiry delay. Health and control-plane routes remain available even if the
journal requires repair; an unreadable journal keeps business admission closed.

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
unfinished streaming response cannot hold the process open. The SSE streams do
not spend that budget: the gateway's server marks its shutdown as it begins and
each stream ends itself within one poll tick, so its client reconnects at once.
ava-root waits for the gateway's stop longer than that budget plus the lifespan
cleanup (`ServiceSpec.stop_ceiling_s`, derived from the same setting), so a
gateway still draining is never reported as a failed stop. No remote
dispatch holds that cleanup: the TTL reaper runs as its own service, whose stop
cancels its in-flight dispatches, and rows left expired are re-selected by its
next start.

The existing home-local journal survives a CLI crash, host reboot and an
offline database. An incomplete drain or stop retains the hold and reports
failure. A drain that hits its deadline reports every unfinished agent — its
restart command's delivery state, the row's owner/lease/resource facts, the
agent's last activity and the live host's view — and names the predecessor-owner
fence explicitly when a successor boot is looking at a row its predecessor left:
that one needs `ava maintenance status` plus an explicit `ava maintenance cancel`
or `repair`, never a retry loop. A rollout's own pause no longer defers the held
continuation it requires, so the ordinary update drain consumes and certifies.
For ordinary local maintenance, retry the command or run `ava start` to restore
services and release the hold after readiness succeeds. A failed start keeps
admission closed.
A recorded checkpoint/continuation failure blocks ordinary start and resume
before services are launched; repair and inspect that failure first. A healthy
service probe cannot prove that a failed checkpoint became durable.
`GET /api/health` and ops `status_probe` remain available during this hold, so
start can measure real readiness before opening business requests or native
admission. Public health still verifies identity and database access; status
and resume retain their existing authentication requirements.
Resume is refused before readiness or after a recorded continuation
failure. Every selected
service must pass readiness; normal start exposes no readiness waiver.
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

`python -m cli.fleet_update down` stops every unit with `ava stop -y` and `up`
starts them again (the runbook's "Updating a networked cluster in source mode").
A running schedule or PTY can retain old code and DB access outside application
root, so root exit alone is not a writer barrier: the stop gives persistent
terminals a bounded completed-work wait, then closes every one (SIGKILL only over
captured births), and each busy owner receives the same closure notice as
`ava stop`. Terminal state does not survive an update; schedules are re-armed
after start.

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
the one-time cutover procedure against the actual running version before
switching it to the new lifecycle.

Cold preparation can retain an expired owned idle row only when its native
consumers are absent, resources are empty and the latest persisted checkpoint
is a complete halted END. Nothing is written: historical leases, identity,
messages, checkpoints and lifecycle acknowledgements are preserved.
An expired lease alone, unfinished lifecycle/graph work or an uncertain
checkpoint still refuses; queued ordinary messages remain available for resume.
Preparation settles orphaned ordinary claims on non-cold parked agents and
bounded-waits unfinished lifecycle commands. Maintenance-authored commands
still refuse immediately. See the
[preparation settlement and wait contract](../base/deploy/maintenance/docs/lifecycle-wait.ava.okf.md)
for eligibility, stale cutoff, and retry semantics.

## Holds and their exits

`ava stop` and `ava restart` take the hold and walk it through drain and stop;
`ava start` releases it after readiness. No command runs those steps one at a
time. `ava maintenance` is only the journal's reader and its two exits, each
local and taking the journal's exact `--operation` and timezone-aware
`--acquired-at`: `status` prints the hold, `repair` releases failed receipts
(below), and `cancel` abandons preparation/drain while services are usable. It is
not for bypassing a partial stop or a failed startup.

`cancel` releases the hold; it does not retract restarts already
issued (durable per-agent intents). Members not yet at their boundary still
complete that restart, with its cold recovery, on next admission.

A drain aborted by failed receipts keeps the hold, and `cancel`
refuses while blocked failures remain. Receipts whose turn raised a
database-outage exception (`psycopg.OperationalError`, `PoolTimeout` — the
crash-equivalent family; every database channel hang surfaces as one of
these) do not block: they are recorded
as undelivered, and after the channel recovers the host re-drives the
held-control path (explicit re-flush, then restart claim) before the drain can
certify. A failed wake of an agent with no continuation left in the hold
records no receipt at all: one outside the captured cohort (another machine's:
every runner receives every wake), or a drained or parked member once the
hold reached `drained`. For genuinely blocking failures, fix the root cause
first, then run
the sanctioned repair:

```
ava maintenance repair --operation <operation> --acquired-at <timestamp> [--operator "Ava #1234"]
```

Repair requires the exact generation capability, works on a
`preparing`/`draining`/`drained` hold (a failure latched after the cohort
landed has no other exit), refuses while the agent-host still has active
continuations. Only independent service, PID and home-scoped process checks
can establish host absence and skip its identity probe; a refused health
connection alone is insufficient. Repair records operator identity (timestamp,
operator label, OS user/uid/pid, parent process, machine) in the journal — both sides of the
repair CAS stay visible via `ava maintenance status`. A partial release after
a successful repair is completed by `cancel`.

## Recovering a stuck maintenance operation

A maintenance hold survives a CLI crash, host reboot and offline database. It
does not expire. There is no pause-controller or OS hold-watchdog recovery job.
A failed or unreadable ownership observation never permits an independent
restart or release of admission.

First identify what owns the home: `ava maintenance status` names the operation
and phase. Do not apply the manual table below over a `cli.fleet_update` half
that is still running; rerun that half after its failure is fixed instead.

For ordinary maintenance, read the exact generation and phase:

```bash
ava maintenance status
```

`cancel` and `repair` use the journal's same `--operation` and timezone-aware
`--acquired-at`. Status includes recorded process identity and judged liveness;
a refused connection alone does not establish that the owner is absent.

| Phase found | Recovery after confirming there is no competing owner |
| --- | --- |
| `preparing`, `draining` | `ava maintenance cancel` abandons the drain while services are usable. |
| `drained` | `ava maintenance cancel`, or re-run `ava stop`. |
| `stopping` | Re-run `ava stop` to verify and finish closure, then `ava start`; or `ava start` directly. |
| `stopped`, `starting`, `ready` | `ava start` brings services back, verifies readiness and releases the hold. |

An unreadable journal, or a `paused` record with no maintenance hold (what the
retired updater's stop left), has no exact generation for these commands to
match, and no command clears it. After confirming no `ava stop` or `ava
maintenance` command is in flight for this home (`ava maintenance status`
reports what it can read), remove `$AVA_HOME/run/deploy-pause-owner.json` by
hand and run `ava start`.

A failed stop retains its process inventory and hold. Force remains an explicit
owned-process escalation, not an inference from a timeout or a way to
manufacture a drain receipt.

An ordinary `ava start` can complete ordinary stopped/starting recovery and
resume after full readiness. Blocking checkpoint/continuation failures refuse
before services launch. Repair those through the exact-generation
`maintenance repair` procedure above. A successful service probe cannot make
an unflushed checkpoint durable.

## Supervision and recovery ownership

Ava root supervises the admitted application roster. Deliberate maintenance
stops remove services from that roster before closing their captured processes;
service supervision cannot reinterpret a planned stop as an unexpected crash.
The ordinary root boot unit owns replacement applications. Data-plane and persistent-terminal
custody are separate and must be reconciled explicitly.

Host startup and successor admission reconcile proven-dead hosted agent owners
through `agent.ownership.hosted.settle_stale_running_rows` and the ordinary
incarnation protocol. Releasing a hold does not itself prove resource closure
or replay arbitrary external effects. Missing or unreadable evidence remains
an unresolved operation.

For a drill that intentionally stops the message plane, keep its progress and
recovery access outside that plane. Record the captured operation, process
identities, current phase, intended resource scope and the actor responsible for
recovery before the stop. A surviving actor may inspect native evidence and
continue the recorded operation; it must not clear a live owner's locks or
invent a second rollout. Preserve failed-phase evidence even after successful
recovery, and verify business admission, selected services and native custody
before declaring the operation complete.
