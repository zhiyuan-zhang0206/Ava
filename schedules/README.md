# Schedules

Gateway-hosted schedules for cluster agents: a schedule is a persistent,
supervised session (a `script` + the `command` that runs it) owned by the
gateway's ScheduleManager — see `gateway/schedule_manager.py`. Manage them via
`ava schedules ...` (thin client over `/api/schedules`) or the
`/control/schedules` page.

## Built-in schedules

Ava ships a set of built-in schedules, declared in
[`manifest.json`](manifest.json) next to their script templates in this
directory. The manifest is the single expression of the built-in policy
(user ruling 2026-08-11, pre-open-source):

| Schedule | Script | Class | Default |
|----------|--------|-------|---------|
| `adversarial-eval-weekly` | `adversarial-eval-weekly-schedule.py` | product | **enabled** |
| `memory-arbiter` | `memory-steward-schedule.py` | product | **enabled** |
| `self-evolution-daily` | `self-evolution-daily-schedule.py` | product | **enabled** |
| `self-evolution-weekly` | `self-evolution-weekly-schedule.py` | product | **enabled** |
| `model-update-tracker` | `model-update-tracker-schedule.py` | product | **enabled** |
| `trace-ship-tempo` | `trace-ship-tempo-schedule.py` | operator | **disabled** (present, not started) |

- **product** schedules (adversarial evaluation, self-evolution, memory, model tracking) are Ava's
  own improvement loops — they ship and start by default.
- **operator** schedules (cluster-operator tooling, e.g. shipping OTel traces
  to a local Tempo viewer) ship with the product but start **disabled**: they
  exist so they are discoverable, and start only when the operator enables
  them.

### How built-ins get created

`provision_builtin_schedules()` (`shared/builtin_schedules.py`) creates every
manifest schedule missing from the `schedules` table, with `enabled` taken
from the manifest's `default_enabled`. It runs:

1. **at every gateway boot** — a fresh install comes up with its built-ins
   (enabled ones are launched by the manager's reconcile loop within a poll
   tick), and
2. **`ava schedules provision`** — manual restore, works even while the
   gateway is down.

Provisioning is **idempotent and non-destructive**: an existing row is never
touched (not even re-enabled), so an operator's edits — script, description,
enabled — survive every boot. Delete a built-in and the next provision brings
it back with its manifest default; `ava schedules stop <name>` (or disable in
the UI) keeps it around without running.

### Adding or changing a built-in

Edit the script template and the manifest entry (name, class, default_enabled,
description), PR it, and deploy. On deploy, new manifest entries are
provisioned at the next gateway boot; changed defaults only apply to rows that
do not exist yet — an existing cluster's rows keep their operator-set state.
To roll a changed template out to an existing cluster's schedule, use
`ava schedules update <id> --script-file <template>` + `ava schedules restart <id>`
as before.

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
  Claims commit before the fire callback: a crash after claiming can lose that
  slot, which is the intentional at-most-once trade-off.
- **A template change does not reach a running cluster on its own.** The DB is
  authoritative and `provision_builtin_schedules` only inserts rows that are
  missing — it never rewrites one that exists — so `ava cluster update` refreshes
  the checkout without touching the script any cluster is actually running.
  Push it explicitly, once per changed built-in:

  ```bash
  ava schedules update memory-arbiter        --script-file schedules/memory-steward-schedule.py
  ava schedules update self-evolution-daily  --script-file schedules/self-evolution-daily-schedule.py
  ava schedules update self-evolution-weekly --script-file schedules/self-evolution-weekly-schedule.py
  ava schedules update adversarial-eval-weekly --script-file schedules/adversarial-eval-weekly-schedule.py
  ava schedules update model-update-tracker  --script-file schedules/model-update-tracker-schedule.py
  ava schedules update trace-ship-tempo      --script-file schedules/trace-ship-tempo-schedule.py
  ```

  An **enabled** schedule is relaunched onto the new script by that call alone;
  a disabled one picks it up when it is next started. `ava schedules get <name>`
  confirms which script the row holds.
- Deploy a one-off schedule with:

```bash
ava schedules create <name> --script-file <script.py> [--description "..."]
ava schedules start <name>
```

The running copy on each host lives in `~/.ava/schedules/<id>/`; this directory
is the version-controlled source of truth — never edit only the running copy.
