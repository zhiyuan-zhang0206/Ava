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
those live sessions must survive. Externally launched tools are not owned by
one local home.

Impersonation is a separate agent identity protocol. These commands do not
request, acquire, renew or release external-agent control leases. Also, the
legacy `ava cluster pause NAME` is machine membership administration, not this
local maintenance operation; do not substitute it for `ava pause`.

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
unfinished streaming response cannot hold the process open. The TTL reaper's
serial remote-dispatch batches stop starting new dispatches once shutdown
begins, so that cleanup waits for an in-flight dispatch, never the remaining
batch — deferred rows are re-selected by the next boot's pass.

The retained release executor calls the ordinary maintenance drain with
straggler reaping enabled. A cohort member still un-landed
`update_straggler_reap_seconds` (default 15) after its restart command was
issued is truncated and released with the honest `reaped` outcome instead of
aborting the wave (task #4016; the local stop family — `ava stop`/`pause`/
`restart` drains — never reaps). Its mark is settled at the next agent-host
boot or local resume (`ava start` runs the resume path), and its claimed
work re-delivers on the new code. A failure recorded while that truncated
turn unwinds (the member losing its row mid-unwind, task #4150) is settled
with the mark: a reaped member's failure never gates resume, stop, start or
repair. A later failure receipt for a reaped member is not latched; a failure
that races before the reap receipt remains audit evidence but does not block.

The existing home-local journal survives a CLI crash, host reboot and an
offline database. An incomplete drain or stop retains the hold and reports
failure. A drain that hits its deadline reports every unfinished agent — its
restart command's delivery state, the row's owner/lease/resource facts, the
agent's last activity and the live host's view — and names the predecessor-owner
fence explicitly when a successor boot is looking at a row its predecessor left:
that one needs `ava maintenance status` plus an explicit `resume --cancel` or
`repair`, never a retry loop. A rollout's own pause no longer defers the held
continuation it requires, so the ordinary update drain consumes and certifies.
For ordinary local maintenance, retry the command or run `ava start` to restore
services and release the hold after readiness succeeds. A captured release
operation instead continues through its exact prepared request; ordinary start
cannot bypass its incomplete journal. A failed start keeps admission closed.
A recorded checkpoint/continuation failure blocks ordinary start and resume
before services are launched; repair and inspect that failure first. A healthy
service probe cannot prove that a failed checkpoint became durable.
`GET /api/health` and ops `status_probe` remain available during this hold, so
start can measure real readiness before opening business requests or native
admission. Public health still verifies identity and database access; status
and resume retain their existing authentication requirements.
The existing exact-generation `cluster_resume` RPC is reachable but refuses
resume before readiness or after a recorded continuation failure. Every selected
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

`ava cluster update --prepared REQUEST` submits one captured operation to the
external release executor. The current adapter supports a single Linux gateway
with a local data plane and identical packaged SQL. It refuses retained terminal
writers before draining. A running schedule can retain old code and DB access
outside application root; its lifetime must be explicitly resolved before the
operation is admitted. Root exit alone is not a writer barrier.

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
is a complete halted END. The same boundary can park a completed legacy
restart stranded in `restarting`: its done, untargeted command must precede
the final exit checkpoint. Only the parked status changes; historical leases,
identity, messages, checkpoints and lifecycle acknowledgements are preserved.
An expired lease alone, unfinished lifecycle/graph work or an uncertain
checkpoint still refuses; queued ordinary messages remain available for resume.
Preparation settles orphaned ordinary claims on non-cold parked agents and
bounded-waits unfinished lifecycle commands. Maintenance-authored commands
still refuse immediately. See the
[preparation settlement and wait contract](../shared/maintenance/lifecycle-wait.ava.okf.md)
for eligibility, stale cutoff, and retry semantics.

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

`resume --cancel` releases the hold; it does not retract restarts already
issued (durable per-agent intents). Members not yet at their boundary still
complete that restart, with its cold recovery, on next admission.

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

Repair requires the exact generation capability, works on a
`preparing`/`draining`/`drained` hold (a failure latched after the cohort
landed has no other exit), refuses while the agent-host still has active
continuations. Only independent service, PID and home-scoped process checks
can establish host absence and skip its identity probe; a refused health
connection alone is insufficient. Repair records operator identity (timestamp,
operator label, OS user/uid/pid, parent process, machine) in the journal — both sides of the
repair CAS stay visible via `ava maintenance status`. A partial release after
a successful repair is completed by `resume --cancel`.

## Recovering a stuck maintenance operation

A maintenance hold survives a CLI crash, host reboot and offline database. It
does not expire. There is no pause-controller or OS hold-watchdog recovery job.
A failed or unreadable ownership observation never permits an independent
restart or release of admission.

First identify the operation that owns the home. For a captured release, inspect
`$AVA_HOME/updates/active` and its operation journal, then submit the same
`ava cluster update --prepared REQUEST`. Submission joins a live executor or
continues only after its prior native ownership is positively closed. The
captured images, phase and recovery direction remain fixed. Native retirement
is verified outside the executor; a CLI return code alone is not completion.
Do not apply the manual table below over an incomplete release operation.

For ordinary maintenance, read the exact generation and phase:

```bash
ava maintenance status
```

Every explicit command uses the journal's same `--operation` and timezone-aware
`--acquired-at`. Status includes recorded process identity and judged liveness;
a refused connection alone does not establish that the owner is absent.

| Phase found | Recovery after confirming there is no competing owner |
| --- | --- |
| `preparing`, `draining` | `ava maintenance resume --cancel` abandons the drain while services are usable. |
| `drained` | Cancel the drain, or complete `maintenance stop`, `maintenance start`, then `maintenance resume`. |
| `stopping` | Repeat `maintenance stop` to verify and finish closure, then start and resume. |
| `stopped`, `starting` | Run `maintenance start`, verify readiness, then `maintenance resume`. |
| `ready` | `maintenance resume` verifies the generation and opens admission. |

On a gateway, `maintenance stop` requires `--gateway-last`, asserting that the
operator independently verified remote stops. Live terminals refuse unless
`--keep-terminals` asserts a separately verified work boundary. A failed stop
retains its process inventory and hold. Force remains an explicit owned-process
escalation, not an inference from a timeout or a way to manufacture a drain
receipt.

An ordinary `ava start` can complete ordinary stopped/starting recovery and
resume after full readiness. Blocking checkpoint/continuation failures refuse
before services launch. Repair those through the exact-generation
`maintenance repair` procedure above. A successful service probe cannot make
an unflushed checkpoint durable.

## Supervision and recovery ownership

Ava root supervises the admitted application roster. Deliberate maintenance
stops remove services from that roster before closing their captured processes;
service supervision cannot reinterpret a planned stop as an unexpected crash.
The external release executor owns release decisions and the ordinary root
boot unit owns replacement applications. Data-plane and persistent-terminal
custody are separate and must be reconciled explicitly.

Host startup and successor admission reconcile proven-dead hosted agent owners
through `agent.hosted_ownership.settle_stale_running_rows` and the ordinary
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
