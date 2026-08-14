# The readiness gate's residual race is accepted, not closed

## Context

PR #956 put a readiness gate in front of Phase B: the rollout orchestrator dials the
gateway through the same `probe_gateway_once` the runners' preflight uses, and only
fans out on an authenticated 200. That closed the **systematic** race — prod's
boot-to-serving exceeds `ava start`'s wait, which returns anyway on expiry, so the
gateway was reliably not serving when Phase B fired and every runner correctly
declined (`refusing self-update: preflight failed - gateway unreachable`).

What survives is the transient one: a gateway that serves, is probed, and then dies
before the fan-out reaches the runners. The runners decline exactly as before, and
the poll reports them.

Two things about that window are worth stating precisely, because both were guessed
at before being measured:

- **It is seconds, not milliseconds.** The runner's gateway dial happens inside `ava
  restart`'s preflight, which the detached updater reaches only after `git fetch &&
  git checkout --force && uv sync`. `shared/deploy_timing.py` measures that whole leg
  at **2-15 s across 44 POSIX samples**; it is unmeasured on Windows.
- **Recovery from a real gateway death is bounded below by 60 s.** Nothing brings the
  gateway back but the gateway watchdog, on a 60 s round
  (`AVA_WATCHDOG_INTERVAL_SECONDS`), plus respawn plus uvicorn binding.

## Decision

**Accept it.** No code changes. The failure is already fast — `POLL_STALLED` exits in
~4 s rather than burning the poll's bound — and already correctly labelled:
`RolloutOutcome.INCOMPLETE`, not the silent `rc=0` that #937 removed. The fleet has
one observed instance of the systematic case (the 2026-07-29 05:09 rollout, fixed by
#956) and zero of this residual one.

## Alternatives rejected

**Widen the gate — re-probe between the gate and each runner's dial.** Moves the
window, does not remove it. There is no probe close enough to the runner's dial to
eliminate the gap, because the gap contains the runner's own checkout and sync.

**A bounded preflight retry in the runners.** Rejected twice, for different reasons,
and the distinction matters:

- *Before #956*, it was the weaker fix for the systematic case: it left the ordering
  bug in place and merely made it usually survivable
  (`cli/commands/_gateway_ready.py`, `cli/commands/_repo.py:_probe_gateway_or_die`).
- *After #956*, that objection is gone — there is no ordering bug left to hide, and
  this is exactly the transient failure a retry is normally the right tool for. So it
  is **not** enough to cite the earlier rejection. Two of the three original
  objections survive on their own: the retry is paid on *every* runner in parallel
  rather than once at the gateway, and it stretches the window in which a host sits
  on a moved checkout with old processes — the half-transitioned state the deploy
  lease exists to protect.
- What settles it is arithmetic rather than either objection. A bound cheap enough to
  be free (3 x 5 s) only rescues a gateway that rebound within 15 s — which means
  uvicorn was still binding, and the gate cannot have returned `SERVING` in that
  state, since it requires an authenticated 200. A bound large enough to outlast a
  real death is ~90-120 s, which is precisely the cost #956 priced and refused.
  **There is no cheap middle.**

Note this does not touch `_probe_gateway_or_die`'s existing 5xx retry, which is a
different question: a 5xx means the gateway *answered*.

## If it is ever observed: the shape a fix must take

Not a runner-side retry. **Re-gate and re-fan-out at the orchestrator, once**: when
the poll returns `POLL_STALLED` for one or more runners, re-run
`await_gateway_serving`, and if it answers `SERVING`, re-POST `/api/cluster/update`
to exactly those hosts and poll again — one retry, no loop.

Why that shape:

- The cost is paid **once, at the gateway, and only on failure**. The healthy path
  stays at one probe and zero sleep, which is the property that made the gate
  affordable in the first place.
- It widens **no** runner's half-transitioned window, because a declined runner is not
  half-transitioned: validate-before-kill means it refused *before* stopping
  anything. This is the objection that kills the runner-side retry, and this shape
  does not have it.
- It keeps the readiness question where #956 put it — asked once, off-box, against
  the address the cluster actually dials — instead of re-introducing a per-runner
  definition of "reachable".
- It does not rescue a gateway that stays dead. That remains `INCOMPLETE`, correctly.

Open design points if it is ever built: a second `spawn_update` on a host that already
checked out the target re-pauses and re-spawns, and it needs a guard against
ping-ponging with the runner's own watchdog self-heal.

## Consequences

- A rollout can still report `INCOMPLETE` with runners that declined through no fault
  of their own, and the operator re-runs `ava update`. That is the accepted cost.
- The accept rests on the failure being **legible**, and today it is legible one level
  short of what an operator needs: a runner that declined and a runner whose updater
  died both surface as `POLL_STALLED`, because `RESTART_DECLINED_EXIT_CODE` never
  travels off the host. Making that reason travel is filed separately and changes no
  deploy behaviour — but if it is never done, this decision is weaker than it reads,
  because "correctly labelled" is load-bearing in the argument above.

---

Forward: the "filed separately" item above landed — the decline reason now travels
off the host on `status_probe` and is rendered next to the stalled host in the
rollout report (`ops/updater_outcome.py`, issue #995). Deploy behaviour is
unchanged, so the accept stands as written; "correctly labelled" now holds at the
level this section said it needed to.

Forward: the residual case was observed on 2026-08-01 (issue #1151) — and it was not
the case this page priced. The gateway did not die on its own; the rollout's *own*
Phase-B leg killed it, because a `gateway,agent-runner` box was included in the fan-out
it was orchestrating. That breaks the "no cheap middle" arithmetic above, whose premise
was that recovery from a post-`SERVING` outage is bounded below by the watchdog's 60 s
round: here the killer restarts it in seconds. The cause is removed rather than
survived, and the runner-side budget this page refused is added behind it as defense in
depth — see `decisions/2026-08-01-the-orchestrator-is-not-its-own-phase-b-target.md`.
The "shape a fix must take" section is untouched by that and stays on the shelf.
