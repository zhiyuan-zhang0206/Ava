# The rollout's readiness waiver is observed, not passed

## Context

`ava start` exits `SERVICES_NOT_READY_EXIT_CODE` when a service it launched never
passes its probe. Two callers must not see that code, and both were given
`--no-readiness-gate` to say so: the boot job (uncapped retry) and the rollout's local
gateway leg (whose readiness question `_gateway_ready` answers off-box, one step
later, better).

The second opt-out cannot work on the rollout that introduces it. `ava update`'s
orchestration runs from the interpreter it started in, which imported
`cli/commands/update.py` **before** `git checkout` moved the tree. The child it spawns
is `.venv/bin/ava` from the new tree. So the parent is old code holding an old
`start_args` string, and the child is new code gating by default — and the parent
reads any non-zero as a failed start and calls `_recover_rc`, which reverts the whole
cluster to last-known-good.

Prod's 2026-07-30 21:09 rollout onto 8bdd366 is that run. Reconstructed from
`rollout-1785470962.log`, the reflog, and pidfile mtimes:

- 21:10:28 checkout to 8bdd366; 21:10:29 the new `ava start` launches every service
  **except** the gateway, whose session it reported as "already running" — a husk left
  behind by the stop's forced kill (`_graceful_kill_session` returns `"forced"` without
  confirming the session died). No gateway process existed.
- The readiness wait ended in ~1 s, not 180: an unready service whose session is
  confirmed gone ends the wait immediately, which is correct and is why the log's
  "within 180s" wording overstates what was spent.
- The child exited 4. The 7e571b4 parent printed `✗ ava start failed` and rolled back.
- 21:11:27 the gateway watchdog found the gateway dead and respawned it; 21:11:29 it
  was serving. The off-box gate's bound is 180 s, so had the child exited 0 the
  rollout would have proceeded.

A target was reverted over a gateway that was serving 60 s later, and no code in the
range had anything wrong with it — `python -m gateway` imports clean at 8bdd366, prod's
`AVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS=90` / `AVA_ALLOCATED_REAP_GRACE_SECONDS=120`
included.

## Decision

A gateway-capable host **waives the readiness verdict whenever it observes a live
update lease**, in addition to honouring `--no-readiness-gate`
(`cli/commands/start.py:_readiness_waiver`).

The flag is a caller *declaring* it owns the readiness question; the lease is the host
*observing* that a rollout owns it. Only the observation survives the skew, because it
lives entirely in the new child and needs nothing from the parent. `ava status` and
`ava stop` already branch on the same row through `_update_in_flight`, so this adds a
consumer to the cluster's single "intentionally mid-transition" signal rather than a
second source of truth for it (`shared/cluster_lock.py`).

Scoped to gateway-capable hosts, and read only on the failure path — a healthy start
must not price itself on a central-DB round trip.

## Alternatives rejected

**Make the gate opt-in.** Skew-proof for the same reason, and it throws away the
change: the default is the whole point, since every programmatic caller that reads
`rc == 0` as "serving" is the bug `SERVICES_NOT_READY_EXIT_CODE` exists to fix.

**Have the updater export an env var instead of a flag.** Identical skew. An old
parent sets no variable, whatever its name.

**Teach `_recover_rc` to ignore code 4.** Fixes this code and no other, in the parent
— which is the process that is old exactly when it matters. Any future divergence
between what the parent sends and what the child does reopens it.

**Waive on every host, not just gateways.** Loses a response worth keeping: an
agent-runner's ladder answers 4 with an idempotent local `ava start`, which repairs
that host and reverts nothing.

## Consequences

- A rollout whose local leg comes up incomplete now proceeds to `_gateway_ready`
  instead of reverting. That gate is strictly better placed to judge (off-box,
  authenticated, the address the runners dial) and still fails the rollout if the
  gateway truly never serves — so the reverting case is narrowed, not removed.
- A service that is unready for a reason Phase B does not depend on (a headless
  `browser`, a slow `milvus`) can no longer revert a cluster. It is still printed, and
  the watchdog keepalive still revives it.
- **This does not fix why the gateway was missing.** The stop reported a forced kill
  it never confirmed, and `_launch_sessions` treats "a session exists" as "the
  service runs", so a husk session makes `ava start` silently skip a daemon. That is
  older than this range and is filed separately; the waiver only stops it from costing
  a cluster-wide revert. (Now fixed —
  `decisions/2026-07-31-a-live-session-is-not-a-running-service.md`.)
