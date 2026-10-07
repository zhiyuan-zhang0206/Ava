# A hung updater is reaped on a schedule, because the next attempt cannot reach it

## Context

`ops/cluster_deploy.py` has always known how to kill a hung `ava-updater`: a session
whose log has not advanced in `NO_PROGRESS_TIMEOUT_S` is blocked on a
non-terminating step, and `_reap_stalled_updater` force-kills it. The call sat in
one place — inside `spawn_update`, on the path a *new* update takes when it finds
the session still there. Its own docstring described the mechanism accurately as
"unblock-the-next-attempt … not a background reaper".

That reads like enough. `ava update`'s Phase-B poll carries its own bound from the
same constant and reports `INCOMPLETE` rather than a silent `rc=0`, so the caller is
protected against a host that never comes back. The remaining cost looked like
latency of *discovery*: the dead session is invisible until someone tries to deploy,
which on a quiet weekend is days.

**Auditing the refusal path says the cost is not discovery latency — it is a
deadlock.** A live `ava-updater` makes `current_orchestration()` answer `"update"`
for as long as it exists, and the two actors that would otherwise have ended it both
read that answer:

- The gateway's next `ava update` calls `_assert_no_orchestration_in_flight`, which
  asks `ops.deploy_window.deploy_in_flight()`. Signal 3 probes every machine's
  `current_orchestration` and reports this host as mid-deploy, so the **whole
  rollout is refused cluster-wide**. Phase B never fans out, and the `spawn_update`
  that carries the only reap call is never reached.
- This host's own **code** controller defers to the same session, on the correct
  general rule that stale processes are a running deploy's normal transient. So an
  on-pin host running old code waits on the corpse with no end.

The pin controller is deliberately excluded from that list: it gates on the
`update_lock_holder()` lease, not on the session, and a watchdog-spawned
`spawn_update` takes no lease — so an off-pin host with no lease outstanding walks
through pin's guards into `spawn_update_local`, whose inline reap clears the corpse.
That leg self-heals today. The two that do not are the cluster-wide refusal and the
on-pin code controller, and they are what this decision is about.

One hung session on one runner therefore blocks the cluster's deploy path *and* that
runner's code recovery, permanently, and the documented exits are `ava update --force` —
which skips the cluster-wide deploy-window check the corpse is impersonating, i.e.
trains the operator to suppress the real protection — or a human with
a session kill. Bounding the Phase-B poll does not touch this: the poll protects
a rollout that *started*, and this refuses the start.

## Decision

**The reap also runs on the agent-runner watchdog's own round**
(`ops/controllers/stalled_updater.py`, `ops.cluster_deploy.reap_stalled_updater_if_hung`),
first in the controller list.

Three properties make the unattended kill a narrow addition rather than a new power:

- **The evidence bar is unchanged.** The same `_UPDATER_STALL_TIMEOUT_S` of silence,
  measured in the same log, by the same function the caller-side reap uses. This is
  a new clock asking an existing question, not a new judgement about when a deploy
  is dead.
- **It runs first, and never blocks.** `spawn_update` pauses the host before it
  spawns, so a hung updater is always found on a *paused* host — and
  `PauseController` blocks with `BlockScope.ALL`, short-circuiting every controller
  behind it. A reaper anywhere after `pause` would be skipped in exactly the case it
  exists for. It is safe first because it deletes a stale signal rather than moving
  a service, so the controllers behind it decide on a corrected reading of the host.
- **It reaps and stops.** No unpause, no restart, no re-trigger. Killing the session
  hands the host back to the controllers that already own those dimensions —
  stranded-pause lifts the pause, pin force-updates the checkout, code restarts
  stale processes — of which the code leg was unreachable *only* because of the
  corpse; the other two gate on the update lease, which the corpse does not hold.
  The unattended power added is "kill one session that has been silent for 15
  minutes", not "recover a host".

Agent-runner only. That is a co-location fact, not a policy: the `ops` daemon is
agent-runner-scoped and `spawn_update` runs inside it, so every host that can hold an
`ava-updater` also runs an agent-runner watchdog. A gateway-only unit runs no ops
daemon and has nothing to reap; adding the gateway leg would be a no-op on a split
deployment and a second killer racing the first on a single box, which runs both.

## Alternatives rejected

**Leave it caller-side and accept the discovery latency.** The argument was that an
unattended destructive action is worth more than the delay it saves, since the
caller is already bounded from its own side. It rests on a premise that does not
hold: the caller is not bounded, it is *refused*, and the refusal is what prevents
the reap. The choice was never "reap now or reap on the next deploy" — it was "reap
now or wait for a human".

**Widen the caller-side reap to every deploy-adjacent entry point** (have
`deploy_in_flight` reap what it finds). Rejected: it makes a read-only question
destructive. `deploy_in_flight` is called by the pin controller, the code
controller, the health probe's auto-rollback suppression and the refusal path — a
side effect there means "asking whether a deploy is running" can kill one, from four
callers with four different notions of how sure they are.

**Have the gateway reap remote sessions during the Phase-B poll.** Rejected: the
kill needs the log's mtime, which is host-local, so the gateway would be killing on
weaker evidence than the host has; and it would only cover hosts inside a rollout
that started, which is the case that already works.

**Put the reap in the OS-scheduled watchdog probe instead.** Rejected: that probe is
deliberately dumb (pidfile alive → exit, dead → respawn) precisely so it cannot have
opinions the controller gates own. Reconcile logic lives in the round.

## Consequences

- A hung `ava-updater` now clears itself within one round of crossing 15 minutes of
  silence, on the host that owns it, with no operator present.
- `spawn_rollout` and `spawn_restart` still refuse outright: they reach
  `_assert_no_orchestration_in_flight` and never get as far as `spawn_update`'s
  inline reap, so unlike an off-pin `spawn_update` they cannot clear the corpse
  themselves. Under this design that refusal stops being a dead end and becomes a
  wait of at most one watchdog round (≤60 s), because the reaper — not the caller —
  owns the corpse.
- `ava update --force` loses its most common legitimate use. That is the point: the
  flag stays for a deploy window that is real but stale (a host powered off
  mid-update), and stops being the routine answer to a corpse.
- A genuinely slow updater that writes nothing for 15 minutes is killed by the
  watchdog where previously only the next deploy would have killed it. The window is
  unmeasured on Windows (no successful Windows self-update exists to measure), which
  is why the constant is a first-principles ceiling rather than a measurement — if
  that ceiling is ever found to be too low, it is one number in
  `shared/deploy_timing.py` and it moves for all four consumers at once.
- The controller list gains a member whose `BlockScope` is always `NONE`. That is a
  new shape — every other controller blocks something when it acts — and it is
  deliberate: this one's action *removes* a reason to hold back.
