# Cluster timezone as a process-wide wall clock

## Context

User ruling 2026-08-27 (Task #1758): the timezone is a cluster-level setting,
and every agent runner must pull it from the gateway instead of relying on its
own machine's OS timezone — a WSL runner whose OS zone was never switched
exposed the mismatch.

Most of the plumbing already existed: `AVA_TIMEZONE`
(`settings.general.timezone`) is a cluster-pinned setting, served to pure
agent-runners through `GET /api/bootstrap` at every process start, and every
agent-facing timestamp (`shared.config.format_timestamp`), the watcher/schedule
cron math, and the per-agent timezone context note already read it. What was
missing was the *process floor*: no process applied the value to its own wall
clock, and a handful of display paths still read the host OS zone directly
(`datetime.now()` / no-arg `.astimezone()` / loguru's `{time}` stamp), so a
runner on a machine whose OS zone differs from the cluster's rendered
machine-local times in logs and operator surfaces.

## Decision

One cluster clock. A configured process that holds an authoritative
`AVA_TIMEZONE` (the gateway unit's `.env`, a runner's bootstrap fetch, a
schedule runner's pinned spawn env) applies it at boot as its own `TZ`:
`os.environ["TZ"]` plus `time.tzset()` on POSIX, from the new
`shared/config/cluster_tz.py` boot hook called right after the Settings
singleton is built. That single step covers every naive local-time read in the
process — `datetime.now()`, no-arg `.astimezone()`, loguru's `{time}` stamp,
`time.localtime()`, and subprocess children that inherit the env.

Display paths that format an instant explicitly (`shared/alerts.format_local`,
`ops.cluster_status` session times, `shared.memory_repo` last-fetch,
`shared.envfile` backup-name stamps, `ava.agents` relative times, the one-shot
watcher announcement) now go through the `cluster_tz()` helper: the cluster
`ZoneInfo` when the process holds an authoritative value, else `None`, which
`.astimezone(None)` treats as the host zone.

A process WITHOUT an authoritative value — a settings-lite maintenance verb
(`ava stop` / `ava status` / the watchdog probe, which must keep working while
the gateway is down), a bare checkout, CI — is deliberately left on its host
zone: there is no cluster clock available, so the host zone is the honest
fallback, and forcing the field default (`America/Los_Angeles`) onto it would
be wrong.

Deliberately unchanged: the startup-only bootstrap fetch (no heartbeat
re-fetch; `AVA_TIMEZONE` is `restart_required: agent`, so a cluster edit
reaches agents on restart, which re-fetches), the schedule runner's pinned
spawn env (now belt-and-braces), the frontend's browser-local rendering (a
deliberate divergence from the audit §4.4 — each audience gets one clock), and
the Postgres/backup UTC discipline.

Windows has no `time.tzset()`: the hook still exports `TZ` for subprocess
children, and the explicit `cluster_tz()` reads keep every display path on the
cluster clock.

## Consequences

- A runner whose OS zone differs from the cluster's now shows cluster time in
  logs, watcher output, IM alerts, status snapshots, and backup names.
- Settings-lite verbs keep working with the gateway down and fall back to the
  host zone, documented.
- `datetime.now()` in new code is cluster-correct by default on any configured
  process; the explicit helper remains for display formatting.
