# `ava.self.update()`: the initiator waits for the quiesce signal instead of restarting itself

> **Superseded 2026-08** — `ava.self.update()` was removed; the only update
> entry point is the CLI `ava cluster update`, whose trigger is a detached
> `ava-rollout` session (not an agent) that never waits on the quiesce. Kept as
> history for why `backend_changed` rides the rollout 202 and how the quiesce
> convergence loop was designed.

**Decision.** On a backend rollout, the agent that calls `ava.self.update()` no
longer inserts its own `self:update` restart and exits immediately. It blocks
inside its `execute_code` (up to 180s, well under the 300s exec timeout)
until a `restart` inbound with `source='system:update'` is pending for it —
the rollout quiesce's own signal, no other source (a concurrent manual restart
must not release the wait, or the process exits while its restarter is still
live) — and only then raises `AgentRestart`. The quiesce signal is used as
proof that the initiator's restarter is paused: the orchestration sends it
strictly after pausing every restarter (local before Phase A, remote in
Phase A). The
202 from `/api/cluster/rollout` now carries `backend_changed` (from the same
`update_check()` preflight that gates the no-op 422) so the SDK knows whether
a quiesce signal is coming at all; frontend-/docs-only rollouts and wait
timeouts keep the old immediate self-restart as fallback. Additionally,
`pause_local_cluster()` moved before the Phase A fan-out.

**Why.** The old flow (fire-and-forget POST + immediate self-restart) raced
the not-yet-paused local restarter and always lost: the initiator exited,
`status='restarting'` was picked up by the restarter's 1s poll, and the agent
was respawned on OLD code within ~1s — 13s before `pause_local_cluster()`
even ran (Phase A blocked 10s on an unreachable host). In the 2026-07-13
incident (agent 405, rollout 062793c5→1d8889d6) the pre-#411 quiesce then
*excluded* the initiator by design ("it already inserted its own restart"),
so the bounced agent rode out the entire rollout live during the migration,
was told "You have been updated and restarted" while nothing had updated,
hallucinated a release announcement, and stayed on old code afterwards. #411
(quiesce convergence loop) fixed the ride-out; this decision removes the
bounce itself: down once, migration runs with the initiator down, one truthful
`system:update` marker on the new code.

**Alternatives rejected.**

- *Gateway pauses the initiator's host restarter synchronously before
  returning 202.* Closes the race too, but adds strand risk (every early-exit
  path — 422, frontend-only fast path, spawn failure, orchestrator crash
  before its unpause guard — must now unpause a flag someone else set; a
  stranded pause also disables the watchdog's self-heal), and a remote
  initiator needs an extra blocking RPC to its host inside the request path.
  The wait design needs no new pause ownership: the signal that already
  exists happens-after the pause by construction.
- *A new "end turn idle" lifecycle primitive (initiator idles; quiesce takes
  it down).* Same ordering guarantee, but requires a new exec-exit primitive
  and leaves the initiator with no feedback path on frontend-only rollouts.
  Rejected per "use existing primitives before adding machinery".
- *Only reordering `pause_local_cluster()` earlier.* Shrinks the window from
  ~13s to the rollout session's ~2s cold start; the restarter polls at 1s, so
  the race is still usually lost. Kept as a hardening measure, not the fix.

**Trade-offs accepted.** The initiator's `execute_code` now blocks ~15s
(typical) on a backend rollout; an aborted rollout costs the initiator a 180s
wait before the fallback bounce. During the transition rollout (old SDK still
running in agents), the old racy path fires one last time and is absorbed by
the #411 convergence loop.


---

Superseded by: [decisions/2026-08-05-cli-only-updates.md](2026-08-05-cli-only-updates.md) — `ava.self.update()` was removed entirely; the CLI is the only update entry point.
