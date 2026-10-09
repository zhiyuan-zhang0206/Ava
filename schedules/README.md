# Schedules

Gateway-hosted schedules for cluster agents: a schedule is a persistent,
supervised session (a `script` + the `command` that runs it) owned by the
`schedule-manager` service's ScheduleManager — see `services/wake/schedule_manager/manager.py`. Manage them via
`ava schedules ...` (thin client over `/api/schedules`) or the
`/control/schedules` page.

## Built-in schedules

Ava ships a set of built-in schedules, declared in
[`manifest.json`](manifest.json) next to their script templates in this
directory. The manifest is the single expression of the built-in policy
(user ruling 2026-08-11, pre-open-source):

| Schedule | Script | Class | Default |
|----------|--------|-------|---------|
| `c9-daily-report` | `c9-daily-report-schedule.py` | product | **enabled** |
| `debt-sweep-daily` | `debt-sweep-daily-schedule.py` | product | **enabled** |
| `dev-ci-metrics` | `dev-ci-metrics-schedule.py` | product | **enabled** |
| `adversarial-eval-weekly` | `adversarial-eval-weekly-schedule.py` | product | **enabled** |
| `memory-arbiter` | `memory-steward-schedule.py` | product | **enabled** |
| `self-evolution-daily` | `self-evolution-daily-schedule.py` | product | **enabled** |
| `self-evolution-weekly` | `self-evolution-weekly-schedule.py` | product | **enabled** |
| `model-update-tracker` | `model-update-tracker-schedule.py` | product | **enabled** |
| `trace-ship-tempo` | `trace-ship-tempo-schedule.py` | operator | **disabled** (present, not started) |

- **product** schedules (adversarial evaluation, self-evolution, memory, model
  tracking, debt clearing, CI observability) are Ava's own
  improvement loops — they ship and start by default.
- **operator** schedules (cluster-operator tooling, e.g. shipping OTel traces
  to a local Tempo viewer) ship with the product but start **disabled**: they
  exist so they are discoverable, and start only when the operator enables
  them.

The adversarial weekly batch owns its counted loopback HTTP server in a lexical
scope. One batch-local executor serves requests while the batch updates the
partner document and collects counts. Leaving the batch, including by an error,
shuts down the server, joins the worker and closes the socket. Server-loop
failures propagate to the schedule's existing batch-error reporting.

### How built-ins get created

`provision_builtin_schedules()` (`base/daemon/schedules/builtin_schedules.py`) creates every
manifest schedule missing from the `schedules` table, with `enabled` taken
from the manifest's `default_enabled`. It runs:

1. **at every `schedule-manager` start** (before its first reconcile) — a fresh
   install comes up with its built-ins (enabled ones are launched by the
   manager's reconcile loop within a poll tick), and
2. **`ava schedules provision`** — manual restore, works even while the
   gateway is down.

Provisioning is **idempotent**. A missing row is created. An existing row whose
`script` or `command` differs from the template is **resynced** to it: the DB copy
of a built-in is a snapshot made when the row was created, and a snapshot goes
stale the moment a library function the script calls changes shape (2026-10-03:
`catch_up()` gained a required `db`, 13 stored copies crash-looped). The resync
writes a `schedule_versions` row (`builtin-resync <hash>`, so the replaced text stays
recoverable) and queues the sync request that relaunches a live session onto the
new code. `enabled` and `description` of an existing row are never touched, so
`ava schedules stop <name>` (or disable in the UI) keeps a built-in around without
running. Delete a built-in and the next provision brings it back with its manifest
default. The checkout is the source of truth for a built-in's code: a hand edit of
its stored script is replaced at the next provision; edit the template instead.
Schedules that are not in the manifest (agent-created ones) are never read or
written by provisioning. Rationale and rejected alternatives:
[decision](../docs/decisions/agents/scheduling/2026-10-03-builtin-schedules-resync-from-repo.md).

### Adding or changing a built-in

Edit the script template and the manifest entry (name, class, default_enabled,
description), PR it, and deploy: the next `schedule-manager` start syncs it into
existing clusters.

The C9 template imports its accounting dependency through
`scripts.ci.pull_requests.accounting`. Materialization changes the script
directory, not the installed module roots. Template tests live in
`schedules/tests/`.

A `.py` template is executed in-process by the runner, which hands it a clean
`sys.argv` — just its own path, exactly what `python <script>` would give it; the
runner's own argv (`python -m gateway.schedules.runner <id>`) never reaches the
script, so a template's CLI takes its own flags only (keep them usable for
manual runs). On deploy, new manifest entries are
provisioned and changed templates are resynced at the next `schedule-manager`
start; changed `default_enabled` / description only apply to rows that do not
exist yet — an existing cluster's rows keep their operator-set state. To push a
template into a running cluster without a restart of the manager, run
`ava schedules provision`.

## Deployment notes

- **The DB is authoritative.** The gateway materializes each schedule's script
  from the `schedules` table to `~/.ava/schedules/<id>/` on launch; editing
  only the on-disk copy is overwritten at the next launch/restart. Change a
  schedule through `ava schedules update` / the API, never by editing the
  materialized file.
- **Cron expressions are cluster wall clock.** The built-in scripts read
  `settings.general.timezone` (`AVA_TIMEZONE`, scope `cluster-pinned`) and pass
  it to `next_fire`, so `0 4 * * *` means 04:00 cluster time on every host in
  the fleet — the host's OS timezone never enters. `AVA_TIMEZONE` is read once
  at process start, so a changed value needs `ava schedules restart <id>`.
  A one-off schedule you write yourself should do the same rather than
  hard-coding an IANA name; `next_fire(expr, timezone=None)` computes in UTC.
- **Built-in cron slots are durably claimed.** Each template calls
  `schedules.catchup.fire_slot_once()` for normal online fires and runs
  `schedules.catchup.catch_up()` once at startup. The claim key is
  `(schedule_id, slot_fire_at)`, so concurrent runners and later restarts cannot
  execute the same slot twice. On first use, the schedule's `created_at` is the
  lower bound; later starts use its newest claim. Startup executes at most the
  two most recent missed slots and warns when older slots were truncated.
  Callbacks receive `(slot_fire_at, payload)`, with the claimed slot normalized
  to UTC, so window-based reports use their own slot even during catch-up.
  Claims commit before the fire callback: a crash after claiming can lose that
  slot, which is the intentional at-most-once trade-off.
- **A template change reaches a running cluster at the next `schedule-manager`
  start** (see "How built-ins get created"), or at once through
  `ava schedules provision`. A resynced **enabled** schedule is relaunched onto the
  new script by the manager's sync request; a disabled one picks it up when it is
  next started. `ava schedules get <name>` shows which script the row holds. A
  one-off change to a single schedule that is not in the manifest still goes
  through `ava schedules update <name> --script-file <file>`.
- **Verify in-store scripts when repo code moves.** `ava schedules verify`
  checks every schedule's DB-embedded script — stopped rows included, built-in
  and agent-created alike — against the checkout it runs from: `py_compile`, a
  top-level-imports-only execution in the checkout's runner venv, a name-read
  check (every name the script reads, any scope, must be one it binds — the
  rename leftover the imports alone cannot see; conservative: any binding
  counts, annotations are not read, `from x import *` disables it), and an
  `inspect.signature().bind` of every call the script makes into repo code
  (`catch_up(...)`, `schedules.catchup.fire_slot_once(...)`, `Database.from_settings()`;
  the argument shape only, so a call it cannot bind statically — `*args`, an
  instance method, a rebound name, the plugin-wrapped `ava.*` SDK — is skipped,
  never guessed). Nothing is started, stopped, or written. The drift classes that
  bit (a module move — e.g. the watcher module's move to `base.daemon.schedules.watcher`
  — leaving stale in-store imports, task #4800; a `shared` -> `base` rename leaving a
  top-level statement reading a name nothing imports — 2026-10-01 audit, three of 16
  copies; a changed signature — `catch_up()` gaining a required `db`, 2026-10-03 —
  leaving calls that raise `TypeError` on the first fire) are caught by its
  `RED id=<id> name=<name> missing=<module|compile-error:<l>:<m>|undefined-name:<l> <name>|call-signature:<l> <call>(): <why>|...>`
  lines before they can fire. `python -m cli.fleet_update down` runs it with the NEW
  checkout's code before stopping anything, and `up` runs it again on the gateway. Exit codes: 0 clean / 1 red / 2 tool error. Run it on the
  host that runs the schedules, or from a dev worktree to check the same table
  against in-development code. `--check-file PATH` checks one script file
  instead of the table (the falsification hook); `--rows-file PATH` sweeps a
  JSON dump of the table (`[[id, name, script], ...]`) with no database access and
  no signal; a non-clean sweep of the live table emits one `schedule_verify_failed`
  event for the Grafana rule to read (`--no-notify` suppresses that for a dry run).
- Deploy a one-off schedule with:

```bash
ava schedules create <name> --script-file <script.py> [--description "..."]
ava schedules start <name>
```

The running copy on each host lives in `~/.ava/schedules/<id>/`; this directory
is the version-controlled source of truth — never edit only the running copy.

## Script repair during maintenance

Normal schedule edits use `ava schedules update`. Business HTTP admission is
closed during the stop/start window, so that command returns `cluster_updating`
while the gateway is held. A stopped cluster can instead use the reviewed
operator tool `scripts/host_ops/update_schedule_script.py`. It accepts only a
schedule ID and script text, requires a failure-free `stopped` maintenance hold,
and holds the same home lifecycle mutex as start/stop. A retained live root does
not invalidate a certified stop. The tool does not advance or release the hold.

For first-generation recovery, copy this one file from a fixed reviewed tool
commit to a private operator directory outside the installed checkout. Run it
with the **existing installed source's own interpreter**, explicit home/source
paths, and the old stored script's SHA-256:

```bash
AVA_HOME="$H" "$S/.venv/bin/python" "$TOOL" "$ID" \
  --home "$H" --source "$S" --script-file "$PREPARED_SCRIPT" \
  --expected-sha256 "$OLD_SCRIPT_SHA256"
```

`H` is the existing gateway home; `S` is its admitted source checkout recorded
in `start-intent.json`. The tool verifies both the interpreter and the loaded
source, and uses that runtime's normal operator database authority. It neither
copies credentials nor imports application code from the tool's development
checkout. It deliberately reuses the already installed schedule writer's
versioned transaction. This narrow bootstrap dependency on
`gateway.schedules.router._fetch_full_blocking` / `_update_blocking` is tested on
`44d7faaa6dc97e237c7c6780cee26f6b1f13703c` and current source; first deploying a
new public writer would recreate the upgrade gate that this entry resolves.
Ordinary edits continue to use the public CLI after maintenance ends.

An edit preserves ID, name, description, command, enabled state, status, error
and creation time. The existing writer updates script/revision/backoff metadata,
adds a `schedule_versions` snapshot, and queues enabled schedules for later
convergence. The tool does not wait for that queue or start/restart sessions.
Prepared text already present is a no-op; any text matching neither the expected
old hash nor the prepared hash is refused. Each invocation commits one row;
a failed batch is partial, and a retry skips completed rows without adding
versions. The old-hash read/write interval is protected by the lifecycle mutex
and stopped admission, not an independent concurrent-writer protocol.

Archive the original rows and versions, validate all prepared scripts against
the fixed application target, and compare the complete stored table after the
edits. Verification includes disabled schedules and the callbacks' argument
contracts; the current static verifier does not prove callback arity by checking
only the outer call. Do not release the hold or launch old schedule code after
installing scripts that require the new callback contract. Finish the normal
whole-cluster update to the approved target before admission resumes.
