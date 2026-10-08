# Production diagnosis playbooks

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Diagnosis playbooks

Each playbook: symptoms → checks → likely causes → response. The cases are
from real incidents; the concrete sizes and dates are illustrative.

### Disk / storage bloat

- Symptoms: disk alarm, checkpoint tables growing, slow queries.
- Checks: `df -h`; checkpoint counts per thread (`checkpoints` group by
  thread_id); blob table size; orphan worktrees / stale cluster homes.
- Causes: LangGraph's PostgresSaver is append-only — terminated agents'
  checkpoint threads grow without bound (a real incident: 21GB in ~12h). Orphaned
  dev-cluster homes an older version left behind (a deleted worktree whose
  cluster was never stopped; a worktree no longer owns one).
- Response: the checkpoint reaper (events-maintenance daemon) owns retention
  automatically: Rule B hourly trims stale threads (terminated, or inactive
  >24h) to keep=1; Rule A on the fast loop trims overgrown active threads
  (>20 ckpts) to keep=5. Compaction-boundary checkpoints are always kept
  (each past compaction segment stays recoverable). Physical space still
  needs VACUUM FULL after large trims (a 42GB→27GB trim was recovered this
  way). Stop a leftover cluster at an orphan home with `AVA_HOME=<home> ava stop -y`
  *then* delete the directory; delete verified-obsolete backups only with
  user approval.

### Memory / heap growth

- Symptoms: OOM kills, agent processes growing, slow respawns.
- Checks: RSS per process; the checkpointer's in-memory state; whether a
  compaction or checkpoint-trim event explains a drop (checkpoint_trim keeps
  N — cross-thread batch deletes are the anomaly, not the norm).
- Causes: unbounded checkpoint accumulation (same as disk), a hot loop
  holding state, a leak in a long-lived daemon.
- Response: attribute the exact consumer and use the smallest authorized
  official lifecycle operation. Never raw-kill a process or bulk-resurrect a
  fleet from a symptom. Pin the regression in CI and verify actual progress.

### Connectivity / port conflicts

- Symptoms: service won't start ("another unit answers on this daemon's
  health ports"), healthz refused, host shows offline while the private
  network is up.
- Checks: `netstat`/`lsof` for the port; identity mismatch message tells you
  which *other* unit holds it; verify the private network / the machine's
  reachable address is up (whatever the operator's VPN overlay client
  reports); machines table `last_seen_at`.
- Causes: two units on one host sharing a default health port (the 8100s are
  a shared segment — pin per-unit ports in `.env`); a Windows system service
  (iphlpsvc) grabbing 8106; an orphaned Chrome holding CDP 9222; stale
  pidfile pointing at the wrong process.
- Response: pin a per-unit port (`AVA_*_HEALTH_PORT`) on the colliding unit;
  for system-service grabs pick a free port; kill the orphan; fix the pidfile
  accounting after stop/start (a missing pidfile leaves services unkillable
  and new instances unable to bind).

### Process / session backend issues

- Symptoms: services dying in a loop, sessions not found, "duplicate
  session" launch failures, watchdog respawn fights.
- Checks: `ava status` (session vs probe columns), session records
  (`run/sessions/*.json`), the session-backend in use
  (`get_backend()` vs `get_shell_backend()`).
- Causes: after the session-backend migration, **service** sessions live in
  `PosixProcSessionBackend` while **PTY** sessions (agent shells, watchers,
  schedules) live in `get_shell_backend()`. Code
  that probes sessions on the wrong backend sees an empty set and relaunches
  into live sessions — the schedule-manager regression: launch
  on the service backend, liveness probe on the service backend → "duplicate session"
  every reconcile tick, breaker tripped. Also: a deleted dev worktree whose
  launchd probe + daemon survived keeps killing the main cluster's open pages
  (same agent-id-derived ports) — tear down the whole unit, not just the dir.
- Response: match the backend to the session type (PTY → `get_shell_backend`),
  kill stale sessions by exact name (`=name`, prefix matching kills siblings),
  remove the whole stale unit (`AVA_HOME=<home> ava stop -y` + bootout probes +
  delete plist).

### Schedules not running / breaker tripped

- Symptoms: schedule launch failures, `status='error'` in `schedules`,
  breaker warnings, a scheduled job (e.g. 4AM consolidation) silently missed.
- Checks: `schedules` table status; gateway log launch failures; runner
  process alive?; the schedule session's pane output.
- Causes: the manager asked the wrong session backend — schedule sessions
  run on `get_shell_backend()` (the pty-sessions service) since the migration
  (step 2: the launch command rides the service's initial-command mechanism,
  so a PTY login shell can run a schedule runner); before that they were raw
  orchestration sessions. A backend mismatch (manager on one backend, session
  on the other) makes reconcile relaunch every tick and collide with the live
  session (`duplicate session` -> breaker trip). Also:
  a stale session from before a rollout that the new gateway cannot adopt; a
  crash-looping script tripping the breaker (`error` is terminal — recovery is
  an explicit API restart/start, which relaunches and resets status).
- Response: clear the stale session (`ava shell kill` the `ava-schedule-N`
  session, or close it through the pty-sessions service), `POST
  /api/schedules/{id}/restart`, verify `status='running'` and the next fire
  fires (check the runner's session log for "Firing:" + the downstream
  effect, e.g. the memory-pool commit). If a restart flips back to `error`
  within ~30s with `duplicate session` launch failures, it is a backend
  mismatch, not a stale session — fix the code, do not keep restarting.

### Message delivery stalled

- Symptoms: `[delivery] inbound ... still pending after 30s` warnings.
- Checks: the target agent's status and machine (`agents_meta`), the
  machine's `last_seen_at` (a pending message usually means the target's
  machine is offline, not a delivery bug), whether delivery resumed after
  the machine returned.
- Causes: target machine offline (its agent cannot start); a wedged
  pub/sub wake.
- Response: if the machine is offline, wait for its return (delivery
  resumes); if it is online but stuck, check the delivery watchdog and the
  agent's own loop. Do not resend from the operator side — that duplicates.
