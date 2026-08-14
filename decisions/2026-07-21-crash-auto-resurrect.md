# Crash auto-resurrect — reconciling an involuntarily-dead agent's stranded inbound

## Context

An agent crash (OOM / SIGKILL / a raw exception kills the process) is detected by
the restarter's corpse reaper, which forces the row to `terminated`. But the
messages queued for it — the one it was processing when it died, plus anything
delivered before or after — stay `pending` in `inbound_messages`. Nothing re-drives
them.

The only existing bring-back path, `resurrect_if_terminated`, is **event-driven**: it
fires only when a NEW inbound is delivered (chat / task / compact). So a crashed
agent is resurrected only if someone happens to send it another message. A peer
waiting on a reply that never comes, or a task whose only trigger already fired,
strands the queue **unboundedly**. This is the tail of *every* crash with no
follow-up traffic, not just a Redis outage.

The fix: a restarter-loop controller that reconciles "an involuntarily-dead agent
still has pending work" the same way `HibernateController` reconciles "a hibernating
agent has pending work" — a machine-scoped scan that resurrects eligible corpses, no
external event required.

## The load-bearing decision: explicit termination source, not a heuristic

A `terminated` row conflates deaths that must be treated oppositely — an intentional
terminate (the user/agent decided it is done) vs. an involuntary death (a crash, a
launch that never came up). Only the involuntary ones should be brought back.

The model carried **no** distinguisher — every path wrote plain `status='terminated'`.
Options considered:

1. **Infer from a `kind='terminate'` inbound.** Rejected — the force-kill path *also*
   inserts a `terminate` inbound (for audit), so "is there a terminate inbound" lies;
   and old terminate rows survive across resurrects, so it is not reliable per-death.
   Inference is exactly the fragility this codebase's fail-fast/explicit posture warns
   against.
2. **A `'crashed'` flag stamped only by the running/idling reaper.** A narrower earlier
   cut. Rejected as under-inclusive and implicit: it left the boot-stage reaps and the
   launch-confirm force as NULL by omission rather than by an explicit taxonomy, and
   "absence means not-a-crash" is the same implicit-signal smell.
3. **An explicit `termination_source` stamped by EVERY terminated-write site.** Chosen.
   A `TEXT` column, `CHECK IN ('user','exit','reaper','launch-confirm')` (NULL
   permitted). Each of the code paths that writes `status='terminated'` stamps its
   source in the same UPDATE:

   | source | write site | eligible? | why |
   |---|---|---|---|
   | `user` | `ops_lifecycle._force_mark_terminated` (force-kill; a terminate that found the pid already dead) | no | the user's will to end it |
   | `exit` | `ops_lifecycle.mark_agent_exited_op` (agent's own graceful process-exit finalize; caught SIGTERM/SIGHUP) | no | intentional self-exit / ops stop |
   | `reaper` | `controllers/respawn.py` — all three corpse reapers (dead running/idling, dead starting, stale allocated) | **yes** | involuntary, system-detected death |
   | `launch-confirm` | `agent_launch._confirm_launch_or_force_terminated` + `_launch_or_force_terminated` (a spawn/resurrect/respawn launch that never confirmed) | **yes** | involuntary; a re-launch is worth attempting |
   | NULL | a pre-migration row, or a write site that did not stamp (e.g. `agent/_starting.py`'s schema-gate boot death, `agents.py`'s respawn-integrity force) | no | conservative default |

   `resurrect_agent` clears it back to NULL on the terminated→allocated transition, so
   the source is strictly per-death and never carries across a resurrect. Each stamp is
   an **unconditional** overwrite, which closes a race: the reaper stamps 'reaper' the
   instant a live agent's pid dies, and if the user then force-terminates that
   already-dead agent (`_force_mark_terminated`, a terminate that found the pid gone),
   the force re-stamps 'user' over the stale 'reaper' — so the user's kill is honored,
   not undone by an auto-resurrect of the still-queued work.

**Why "everything unstamped is NULL and NULL is never resurrected" is the whole safety
model.** A future terminated-write site that forgets to stamp leaves NULL → not
eligible → it can only ever *fail closed* (a missed resurrect), never *fail open* (a
wrong resurrect). And existing rows at deploy time are all NULL, so the new behavior
applies only to rows stamped after the code lands — a safe, automatic gradual rollout.

A `SIGKILL`/`OOM` leaves no `finally`, so it never reaches `mark_agent_exited_op`
('exit'); the reaper catches that dead pid and stamps 'reaper'. A caught
`SIGTERM`/`SIGHUP` runs the exit finally → 'exit'. That split — uncaught death →
'reaper' (resurrect), graceful/ops exit → 'exit' (respect) — is exactly the intended
target population.

## Pending-inbound filter

An explicit **work-bearing allowlist** — `chat` + `compact_request` — not a
terminate/cancel blacklist. The allowlist is fail-closed (the same principle as the
NULL termination_source default): a future inbound kind does not silently start waking
corpses; it has to be added here deliberately. These two are exactly the kinds a *live*
delivery resurrects for (`chat` on the delivery path — task fires deliver as chat too —
and `compact_request` on the compact path), so the controller wakes a crashed agent for
precisely the pending work a live delivery would have. Every other kind is a control
signal (`terminate` / `cancel` / `restart`), a lifecycle marker (`resurrect` /
`restart_completed` / `fork`), a self-generated artifact (`compact_summary`), or a
nudge (`heartbeat`) — none justifies reviving a dead agent to run it (reviving a corpse
whose only pending inbound is a `terminate` to process its own kill signal is the
degenerate case this rules out). A real-work inbound queued next to a `terminate` still
qualifies (the real work wins).

## Churn control: per-agent persistent backoff, no hard cap

A resurrect can fail two ways that both risk a tight loop: the launch fails outright
(an outage that crashes the boot → re-stamped 'launch-confirm'), or it succeeds and the
agent crashes again on the same poison message (→ re-stamped 'reaper'). Both must be
spaced.

`last_resurrect_at` is the per-agent backoff clock — the persistent, per-agent analog
of the pin-heal backoff (which uses an on-disk file per host). The claim is a single
atomic UPDATE that stamps `last_resurrect_at = now()` on exactly the rows it selects,
so a corpse is skipped until `AVA_AUTO_RESURRECT_BACKOFF_SECONDS` (default 5 min) has passed
— whether the last resurrect failed to launch or launched-then-recrashed. Both churn
modes are covered by one mechanism.

**Deliberately no hard attempt cap.** A cap cannot distinguish "temporarily
unstartable" (a transient outage) from "permanently unstartable" (a poison message),
so it would strand the very work this fixes after an outage longer than
`cap × backoff`. Without a cap, the retry is a fixed cadence: a transient outage
self-heals once it clears, and a genuinely-unstartable agent is retried at most once
per window (each a loud WARN, ops-visible) rather than looped or silently stranded. A
human's manual terminate (→ `termination_source='user'`) permanently stops the
retries.

The scan is throttled to the reaper's 30s cadence: a corpse is not even stamped
'reaper' until the reaper's own 30s pass, so scanning faster finds nothing new.

## Placement: the restarter, synchronous `resurrect_agent`

The controller runs in `services/restarter/daemon.py`'s per-tick controller list,
beside `RespawnController` and `HibernateController`. It reuses the **synchronous**
`ops.agents.resurrect_agent` (exactly as `RespawnController` reuses `respawn_agent`),
not the gateway's async `resurrect_if_terminated`: the async path forwards a resurrect
to a *remote*-homed agent's machine, but this scan is machine-scoped (only local
agents), and the restarter is a separate process from the gateway, so the
"blocking the event loop deadlocks the boot-time config fetch" hazard that forces
`resurrect_if_terminated` off-thread does not apply here.

Gated on gateway health (a resurrected agent self-fetches config from the gateway at
boot; resurrecting while the gateway is down would crash-loop it), checked *before* the
claim so an outage does not burn the backoff clock; and on `AVA_AUTO_RESURRECT_ENABLED`
(a cluster-wide kill switch, defaulting on).

## Boundaries / known limitations

- **A boot-stage death is not resurrected by this scan** (`agent/_starting.py`'s
  schema-gate leaves NULL): resurrecting a behind-schema boot would just re-hit the
  gate. That is a deploy/ops problem for a human, not an auto-retry.
- **A crash that leaves no pending inbound is not resurrected** — there is nothing to
  bring it back *for*; it stays a visible corpse.
- **No e2e** covers the real crash → real relaunch chain across processes; the unit
  tests stub the process launch.
