# Stop, restart, and resume a cluster

Use the `ava` belonging to each unit's checkout. `ava stop`, `ava restart`, and
`ava start` act on **one local home**; they do not stop every machine remotely.
For a planned cluster outage, coordinate the participating machines through
SSH or their existing operator entry points. Stop runners before the gateway;
start the gateway and verify its dependencies before starting runners.

## Choose the resource scope

| Command | Native agent execution | Persistent shells and schedules | Local infrastructure and extras |
| --- | --- | --- | --- |
| `ava stop -y` | Same normal drain | Close shells/terminals with bounded known-group signals; report known job leftovers | Stop this home's services, browser, Gate, helper, native LGTM and private data plane |
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
([decision](../../decisions/data/database/2026-10-02-pg-stop-escalates-to-immediate.md)); for terminals, `stop` hangs up each shell's
known shell and signals known shell/foreground groups, escalating after a
grace of at most 10 seconds. Shell survival fails closure; known job leftovers
are diagnostic, and detached/background process disappearance is not certified. A busy
session still leaves its owner the closure notice, written to the database
in the `terminals` phase, before the data plane stops
([decision](../../decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md),
[notice write](../../decisions/agents/messages/2026-10-02-close-notices-written-at-terminals.md)). `--force`
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
that one is ended by `ava start`, never a retry loop. A rollout's own pause no longer defers the held
continuation it requires, so the ordinary update drain consumes and certifies.
For ordinary local maintenance, retry the command or run `ava start` to restore
services and release the hold after readiness succeeds. A failed start keeps
admission closed.
A recorded checkpoint/continuation failure keeps the hold and fails every
other gate (the drain, the phase transitions, a bare resume); `ava start` is its
exit, and settles it as described under "Holds and their exits". A healthy
service probe cannot prove that a failed checkpoint became durable: the agent
continues from its last durable checkpoint, as after a crash.
`GET /api/health` and ops `status_probe` remain available during this hold, so
start can measure real readiness before opening business requests or native
admission. Public health still verifies identity and database access; status
and resume retain their existing authentication requirements.
Resume is refused before readiness. Every selected
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
terminals a bounded completed-work wait, then closes known shells/terminals
best effort. This does not prove detached/background writers disappeared;
inspect residual writers before treating it as an external-work barrier. Each
busy owner receives the same closure notice as
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
[preparation settlement and wait contract](../../../base/deploy/maintenance/docs/lifecycle-wait.ava.okf.md)
for eligibility, stale cutoff, and retry semantics.

## Holds and their exits

`ava stop` and `ava restart` take the hold and walk it through drain and stop;
`ava start` releases it after readiness. No command runs those steps one at a
time, and no command reads or ends a hold except these: `ava status` prints the
hold (phase, operation, acquired-at, recorded failures, the shepherd's
liveness), `ava status --json` prints only it as one JSON object (the fleet
update's start-of-work refusal and the out-of-band triage read that; it probes
nothing, so it answers with everything else down), and `ava start` ends it.

`ava start` over a hold in any phase brings the unit's services up (a unit whose
services are already running is left as it is), then releases the hold. It does
not retract restarts already issued (durable per-agent intents): members not yet
at their boundary still complete that restart, with its cold recovery, on
admission.

A drain aborted by failed receipts keeps the hold, and `ava stop` and `ava
restart` refuse to continue past them. Receipts whose turn raised a
database-outage exception (`psycopg.OperationalError`, `PoolTimeout` — the
crash-equivalent family; every database channel hang surfaces as one of
these) do not block: they are recorded
as undelivered, and after the channel recovers the host re-drives the
held-control path (explicit re-flush, then restart claim) before the drain can
certify. A failed wake of an agent with no continuation left in the hold
records no receipt at all: one outside the captured cohort (another machine's:
every runner receives every wake), or a drained or parked member once the
hold reached `drained`.

A blocking failure is a continuation that raised, so its final checkpoint may
not be durable. The agent's restart pointer is: it survives in Postgres exactly
as after a crash, and the agent continues from its last durable checkpoint
(`ava stop --force` and a host crash give the same guarantee, no more). Once the
unit serves, `ava start` settles each failed receipt before it releases the hold:

- the restart pointer is still pending or claimed: the release's resume
  re-delivers it, with every other member's;
- the pointer is gone (its command finished, or the agent row is): the failure
  is logged at ERROR with the agent, category and hold, and an alert row plus an
  IM push tells the owner through the same channel `ava start` uses for a service
  that missed its window. The agent has no continuation left to deliver;
- the agent is terminated: nothing is owed, nothing is revived.

A start that fails before the unit serves settles nothing and keeps the receipts
for the retry. Undelivered receipts are left in the journal for audit.

## Recovering a stuck maintenance operation

A maintenance hold survives a CLI crash, host reboot and offline database. It
does not expire. There is no pause-controller or OS hold-watchdog recovery job.
A failed or unreadable ownership observation never permits an independent
restart or release of admission.

First identify what owns the home: `ava status` names the operation and phase.
Do not apply the table below over a `cli.fleet_update` half that is still
running; rerun that half after its failure is fixed instead. Status includes the
recorded process identity and its judged liveness; a refused connection alone
does not establish that the owner is absent.

| Phase found | Recovery after confirming there is no competing owner |
| --- | --- |
| `preparing`, `draining`, `drained` | `ava start` abandons the drain and releases the hold; or re-run `ava stop`. |
| `stopping` | Re-run `ava stop` to verify and finish closure, then `ava start`; or `ava start` directly. |
| `stopped`, `starting`, `ready` | `ava start` brings services back, verifies readiness and releases the hold. |

An unreadable journal, or a `paused` record with no maintenance hold (what the
retired updater's stop left), has no exact generation to match, and no command
clears it. After confirming no `ava stop` is in flight for this home (`ava
status` reports what it can read), remove `$AVA_HOME/run/deploy-pause-owner.json`
by hand and run `ava start`.

A failed stop retains its process inventory and hold. Force remains an explicit
owned-process escalation, not an inference from a timeout or a way to
manufacture a drain receipt.

An ordinary `ava start` completes stopped/starting recovery and resumes after
full readiness, settling recorded continuation failures as above. A successful
service probe cannot make an unflushed checkpoint durable; that is why the
agent continues from the last durable one.

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
