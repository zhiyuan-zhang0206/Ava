# Stage-level no-progress clock and the formal rollout cancel

## Context

A live updater lease keeps `_probe_verdict` reading "still working" for up to
`NO_PROGRESS_TIMEOUT_S` (900 s): the lease is one write at the updater's start,
never renewed, so a host stuck inside a single stage — a hung `uv` download on
the Windows runner (2026-08-30 15:14 rollout, `rollout-1788074072.log`: `uv`
took 449.2 s and Phase B polled 505.3 s; the hung variant burns the whole bound)
— holds the global rollout for the full window, and its host only reaps the
updater when the lease finally expires. The only operator escape was a hand kill,
which strands the cluster mid-transition (no `finally`, lease held until TTL).

`NO_PROGRESS_TIMEOUT_S` cannot be tightened to fix this: it is calibrated to the
whole Windows *leg* (~11 minutes observed), and two clocks disagreeing about
"stopped making progress" is the bug class `shared.deploy_timing` exists to
prevent.

## Decision

1. **One new clock — `STAGE_NO_PROGRESS_TIMEOUT_S = 675.0`** — answers a
   different question than the whole-run number (one stage vs the whole leg),
   the same carve-out `GATEWAY_READY_TIMEOUT_S` already holds. Value follows the
   family's own calibration convention: ~1.5x the longest single stage observed
   (449.2 s), strictly inside `NO_PROGRESS_TIMEOUT_S` (lattice-constrained) so
   the judgment fires while the poll still has patience, and strictly above any
   stage a healthy host has ever shown — the false-positive line.
2. **One definition, two consumers, same evidence.** The updater log's last
   `t=` stage marker (both platforms now print one at each stage entry) names
   the in-flight stage; its age, computed on the host's monotonic clock, rides
   the status probe (`UpdaterOutcome.current_stage` / `current_stage_s`).
   - the Phase-B poll returns a new `POLL_NO_PROGRESS` verdict after two
     consecutive probes report a stage beyond the bound — the poll exits for
     that host, and the settle hold (lease safety) covers it;
   - the host's own hung-updater reaper kills an updater whose stage evidence
     exceeds the same bound — on its second consecutive watchdog round reading
     that same stage stuck (the reaper's half of the poll's two-probe
     confirmation; `spawn_update`'s inline reap stays single-read) — and the
     reap now **clears the updater lease** so the host stops reading "live
     updater" the moment the session is gone.
   Old-commit hosts answering without the fields read as "cannot tell" — the
   judgment arrives with the rollout after the one that ships it, the same
   one-rollout lag `last_updater_outcome` itself had.
3. **Formal cancel = SIGINT, never a kill.** `ava cluster cancel`
   (`ops.ops_cluster.cluster_cancel_op`) interrupts the orchestration's own pid,
   read from the deploy lease's holder string (`holder_pid_if_local`, the same
   parser the stalled-rollout controller uses). The orchestration's `finally`
   is the recovery — compensating resume, lease release or settle hold, durable
   maintenance marker cleared — so a cancel leaves exactly the state a normal
   abort would. It refuses on every unprovable reading (no session, settle
   hold, kind-less lease, foreign holder, dead pid), each naming its own next
   step.

## Alternatives rejected

- **Tighten `NO_PROGRESS_TIMEOUT_S` itself** — recalibrates the whole-run
  patience the Windows leg legitimately needs, re-opening the 120 s era's
  "protection expires mid-operation" failure.
- **Renew the updater lease on a timer (host-side)** — a hung stage blocks the
  main flow but a renewal timer thread would keep re-arming, converting the
  lease back into a pure liveness claim; progress-based evidence is the only
  sound signal, and it is what the markers already carry.
- **Kill the rollout session for cancel** — destroys the only process that can
  run the recovery; the cluster sits paused until the lease lapses (the exact
  failure a hand kill produces today).
- **Gateway-side stage comparison against a shrunk bound** — rejected only in
  its standalone form: the verdict must fire on the *host's own* evidence
  (host-monotonic age) rather than the gateway's cross-probe subtraction, so
  the two consumers share one definition and one number.
- **A remote cancel op dispatched to runner updaters** — the hung-updater case
  is already covered by the unattended reaper; an operator-facing per-host
  cancel adds surface without a distinct failure it would fix.

## Consequences

- A stage that legitimately runs past 675 s is judged no-progress by the poll
  after two consecutive probes; the host's reaper adds one watchdog round of
  confirmation, so a stage that finishes inside that gap clears its own
  evidence and is never reaped. When the reap does land — on a stage that has
  been stuck for a full round past the bound — the host may already be
  mid-stop (a stuck `restart` stage), so "keeps serving" is not guaranteed;
  either way the controllers' stranded-pause / code ladder re-triggers the
  update: a retry, not an outage. The 449.2 s observed worst keeps 1.5x
  margin; a fleet that grows slower stages retunes this one clock.
- The cancel verb is scoped to rollout/restart orchestration on the host that
  runs them; rollbacks, settle holds and host self-updates keep their existing
  recovery paths.
