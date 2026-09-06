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
already-admitted handlers and executor work before signalling services.

The existing home-local journal survives a CLI crash, host reboot and an
offline database. An incomplete drain or stop retains the hold and reports
failure. Retry the command, or run `ava start` to restore services and release
the hold after readiness succeeds. A failed start keeps admission closed.
A recorded checkpoint/continuation failure blocks ordinary start and resume
before services are launched; repair and inspect that failure first. A healthy
service probe cannot prove that a failed checkpoint became durable.
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
