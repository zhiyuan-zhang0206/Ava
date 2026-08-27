# Cluster timezone as a process-wide wall clock

A configured process now applies the cluster's `AVA_TIMEZONE` to its own wall
clock at boot (`os.environ["TZ"]` + `time.tzset()` on POSIX), so every naive
local-time read — `datetime.now()`, no-arg `.astimezone()`, loguru's `{time}`
stamp, subprocess children — follows the cluster clock instead of the host's
OS zone. A WSL runner whose OS zone was never switched no longer renders
machine-local times.

Display paths that format an instant explicitly (IM alert timestamps,
`ava cluster status` session times, memory last-fetch, `.env` backup names,
relative times, the one-shot watcher announcement) now render through the
`cluster_tz()` helper: the cluster zone when the process holds an
authoritative `AVA_TIMEZONE`, else the host zone (settings-lite maintenance
verbs, which must work while the gateway is down).

The configuration and distribution plumbing already existed (cluster-pinned
setting + per-start bootstrap fetch); this change is the process floor that
makes the setting actually take effect everywhere.

The rationale and the deliberately-unchanged surfaces (startup-only fetch,
schedule env pin, browser-local frontend, UTC data plane) are recorded in
[`decisions/2026-08-27-cluster-timezone-wall-clock.md`](../../../decisions/2026-08-27-cluster-timezone-wall-clock.md).
