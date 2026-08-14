---
name: operating-ava-cluster
description: Day-to-day production operations for the Ava cluster — alert triage, diagnosis playbooks for the recurring failure classes (disk, memory, connectivity, processes, schedules, message delivery), and incident discipline (stop the bleed first, root-cause after). Use when reviewing alerts, diagnosing a production symptom, or deciding how to respond to an incident.
---

# Operating the Ava cluster — production diagnosis playbook

This skill is the cluster operator's day-to-day companion: how to review the
alert stream, how to diagnose the recurring failure classes, and how to
respond. It is **methodology** (how to find and judge), not a fix index —
for symptom → fix recovery of a broken cluster use
[`recover-a-cluster`](../recover-a-cluster/SKILL.md); for the CLI verbs see
`ava-guide/ops`; for rolling out changes see `roll-out-a-cluster-update`.

## Role

- The cluster operator owns: disk / worktree / runtime health, opening and
  closing clusters, node recovery (detect + resurrect), and **rollouts are
  executed only by the operator** (no agent self-updates).
- Node anomaly detection and resurrection are centralized on the operator —
  any agent finding an anomaly reports to the operator, not to the peer that
  discovered it.
- Alert review runs on a schedule (watcher, e.g. every 2h window): triage the
  window's new alerts, classify, and record the disposition. Every alert must
  end up in one of: **known/closed** (already handled — with the incident
  reference), **transient** (self-resolved, no action), or **action needed**
  (investigate now).

## Alert review workflow

1. **Classify by attribution first** — pull the alert list for the window,
   group by service/pattern, and match each group against known incidents
   (this skill's playbooks, memory notes, recent state files). A cluster of
   alerts that maps to an already-handled incident is *known/closed* — record
   it and move on; do not re-investigate.
2. **Transient vs persistent** — a one-shot warning (a dropped SSE event, a
   single slow DB acquire, a statement timeout) is transient: note it, no
   action. A *repeating* pattern (same service, same error, several times) or
   a *state* that outlives the window (a breaker tripped, a host paused) is
   persistent: investigate.
3. **Trace the root-cause chain, not the symptom** — repeated respawns of the
   same service are usually a *cause* elsewhere (a port held by another unit,
   a session backend mismatch, a dead parent). Ask "what keeps killing it?"
   before fixing the respawn itself. Check the machine the event names — a
   query that does not filter by machine can misattribute a win/wsl event to
   the wrong host (seen repeatedly).
4. **Verify the fix, then close** — after an intervention, confirm the alert
   stops (zero new occurrences past the intervention timestamp) before
   marking closed. Record the incident (what, why, fix, prevention) in the
   operator's memory or state file so the next review recognizes it.

## Diagnosis playbooks

Each playbook: symptoms → checks → likely causes → response. The cases are
from real incidents; the concrete sizes and dates are illustrative.

### Disk / storage bloat

- Symptoms: disk alarm, checkpoint tables growing, slow queries.
- Checks: `df -h`; checkpoint counts per thread (`checkpoints` group by
  thread_id); blob table size; orphan worktrees / stale cluster homes.
- Causes: LangGraph's PostgresSaver is append-only — terminated agents'
  checkpoint threads grow without bound (a real incident: 21GB in ~12h). Orphaned
  dev-cluster homes (a deleted worktree whose `ava cluster down --path` was
  forgotten).
- Response: the checkpoint reaper (events-maintenance daemon) owns retention
  automatically: Rule B hourly trims stale threads (terminated, or inactive
  >24h) to keep=1; Rule A on the fast loop trims overgrown active threads
  (>20 ckpts) to keep=5. Compaction-boundary checkpoints are always kept
  (each past compaction segment stays recoverable). Physical space still
  needs VACUUM FULL after large trims (a 42GB→27GB trim was recovered this
  way). Remove orphan worktrees with `ava cluster down --path`
  *then* delete the directory; delete verified-obsolete backups only with
  user approval.

### Memory / heap growth

- Symptoms: OOM kills, agent processes growing, slow respawns.
- Checks: RSS per process; the checkpointer's in-memory state; whether a
  compaction or checkpoint-trim event explains a drop (checkpoint_trim keeps
  N — cross-thread batch deletes are the anomaly, not the norm).
- Causes: unbounded checkpoint accumulation (same as disk), a hot loop
  holding state, a leak in a long-lived daemon.
- Response: heap forensics on PG (varlena compression, tuple layout — see
  pg-heap notes), kill-and-respawn the offender, pin the regression with a
  test before calling it fixed.

### Connectivity / port conflicts

- Symptoms: service won't start ("another unit answers on this daemon's
  health ports"), healthz refused, host shows offline while tailscale is up.
- Checks: `netstat`/`lsof` for the port; identity mismatch message tells you
  which *other* unit holds it; `tailscale status` for the machine; machines
  table `last_seen_at`.
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
  remove the whole stale unit (`ava cluster down --path` + bootout probes +
  delete plist).

### Schedules not running / breaker tripped

- Symptoms: schedule launch failures, `status='error'` in `schedules`,
  breaker warnings, a scheduled job (e.g. 4AM consolidation) silently missed.
- Checks: `schedules` table status; gateway log launch failures; runner
  process alive?; the schedule session's pane output.
- Causes: the manager asked the wrong session backend — schedule sessions
  run on `get_shell_backend()` (PTY supervisor) since the migration (step 2:
  the launch command rides the daemon's `cmd_b64` initial-command mechanism,
  so a PTY login shell can run a schedule runner); before that they were raw
  orchestration sessions. A backend mismatch (manager on one backend, session
  on the other) makes reconcile relaunch every tick and collide with the live
  session (`duplicate session` -> breaker trip). Also:
  a stale session from before a rollout that the new gateway cannot adopt; a
  crash-looping script tripping the breaker (`error` is terminal — recovery is
  an explicit API restart/start, which relaunches and resets status).
- Response: clear the stale session (`ava shell kill` the `ava-schedule-N`
  session, or remove it via the PTY supervisor CLI), `POST
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

## Post-rollout verification checklist (accumulated from real rollouts)

`cluster status` aligned is NOT enough — pid probes only check existence, not
code version. After every rollout, verify:

1. **Service process start times ≈ deploy completion time** on every host:
   `ps -eo pid,lstart,command | grep <home>/source` — a process started before
   the rollout window is running stale code (three real cases: the mcp
   daemon, the wsl watchdog, and the win browser daemon were all found this
   way).
2. **mcp-daemon**: exactly ONE `_mcps_daemon` per unit
   (`pgrep -fc "python -m ava._mcps_daemon"`; wsl co-located units → 2 total).
   Ghosts accumulate when a respawn storm relaunches while the old detached
   process survives; a ghost's exit can steal the live socket.
   Healthcheck probe: `.venv/bin/python -m services.healthchecks.mcp_daemon`
   must log "alive, no-op".
3. **watchdog processes** (agent-runner/gateway) restarted with the new code:
   start time must be ≈ deploy time; a stale watchdog runs old code forever on
   wsl (probe only checks existence). Fix: kill old → probe respawns in ≤1min.
4. **win browser daemon**: restarter does not keep it alive; an empty pidfile
   means only `ava start` relaunches it.
5. **schedules**: `ava schedules ls` — a stale `ava-schedule-*` session from
   before the rollout duplicates the new one and trips the breaker.
6. **gate** (launchd): outside watchdog coverage — kickstart if the
   rollout replaced its code.

## Response discipline

1. **Stop the bleed before the root cause.** An incident gets an immediate
   mitigation that restores service (kill the loop, pin the port, mark the
   row terminal, restart the service) even before the fix is understood.
   Then root-cause and PR the fix. The mitigation is temporary and labeled
   as such in the task.
2. **User approval boundaries.** Rollouts/cluster updates need user approval
   (a standing ruling; offline-tolerance carve-outs are granted separately).
   Irreversible actions (deleting data, force-pushing, killing production
   processes) also need user sign-off — one approval is scoped to one action.
   Emergency *restoration* (stopping a crash loop, freeing a stuck port) is
   within the operator's authority; *changes* (new pins, new config) are not.
3. **Verify, then record.** Every incident ends with: the alert stream quiet,
   the fix deployed or queued, and a memory/state note that makes the next
   alert review recognize the pattern (this is how "unknown alert" becomes
   "known/closed").
4. **Learned constraints are memory, not lore.** When a diagnosis reveals a
   durable environment fact (a port a system service grabs, a backend
   mismatch class, a machine's offline pattern), write it to the operator's
   memory so a fresh process inherits it.
