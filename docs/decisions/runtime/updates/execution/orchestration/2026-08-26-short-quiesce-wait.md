# Cluster-update quiesce window shortened to a configurable 10s default

## Context

The 2026-08-05 two-mode drain priced 'smooth' as "wait out the longest
possible single execute_code": the smooth quiesce window was
`exec_timeout_seconds × 1.2` (default 300×1.2 = 360s), so a healthy agent's
in-flight exec was guaranteed to end at its turn boundary before the
force-reap backstop fired. That guarantee came at a hard cost: every backend
rollout stood still for up to ~6 minutes while agents finished execs — the
cluster stayed blocked (rollout lock, paused restarters) the whole time.

## Decision

Per user ruling 2026-08-26: **an interrupted agent task is acceptable; a fast
cluster unblock is worth more.** Two changes:

1. **The mechanism allows arbitrarily short waits — no minimum.** The smooth
   window is no longer derived from `exec_timeout_seconds`; it is the
   configured `AVA_UPDATE_QUIESCE_TIMEOUT_SECONDS` (default **10s**, user's
   5/10/15s range, taken at 10). Any non-negative value is legal; 0 means
   signal-then-immediately-reap. There is no lower-bound validation.
2. **The default is 10s.** Agents idle or between turns still exit at their
   turn boundary inside the window and are never force-reaped; an agent
   mid-`execute_code` (up to 300s) is now cut short by the force-reap and its
   work lost — accepted per the ruling.

`force` mode is unchanged (~10s wait, always force-reap); the two modes now
differ only in the always-reap axis, not in the wait. The same window applies
to `ava cluster rollback` and the standalone per-host self-heal
(`ava restart --quiesce`), which share `_quiesce_timeout_s("smooth")`.

## Alternatives rejected

- *Keep the exec-derived window but let operators lower `exec_timeout_seconds`.*
  That knob is an agent-runtime safety bound (the sandbox kill), not a rollout
  pacing knob; coupling the two again would re-introduce the minimum-wait
  constraint the ruling removes.
- *A third 'quick' mode instead of changing smooth.* The user asked for the
  default wait to be short, not for another opt-in; a mode nobody passes
  would leave the 360s default standing.

## Consequences

- Backend rollouts unblock the cluster in ~10s instead of up to ~6min; an
  agent cut mid-exec loses that exec's work (its state and memory survive —
  the process is CAS-marked 'restarting' and respawned on the new code).
- The guarantee "healthy agents always finish their current exec" is gone;
  the guarantee "the rollout is bounded and every agent lands on the new
  code" is unchanged.
- Operators who want the old grace can set
  `AVA_UPDATE_QUIESCE_TIMEOUT_SECONDS` higher; the field is cluster-pinned
  and writable through the config surface.
