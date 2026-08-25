# Rollout agent resume boundary

## Context

Phase A pauses every restarter before agents quiesce, and the rollout contract
says those agents remain down until the new gateway is ready and Phase B brings
the fleet back. The gateway's local child violated that boundary: its fresh
`ava start` launched the local restarter alongside the gateway. Agents marked
`restarting` could therefore relaunch while the surviving parent still had to
adopt changed credentials, persist the pin, prove gateway reachability, and
update remote runners. During the credential-split failure this surfaced as
agents starting into gateway timeouts.

## Decision

The local gateway start transiently disables the restarter while launching the
rest of the new service tree. The existing orchestration `finally` remains the
single resume boundary: `unpause_local_cluster()` clears the local posture and
starts the restarter after Phase B, or after compensating recovery on any
failure. The skip is not persisted as operator intent, and the watchdog can
still recover a missing restarter if the orchestration is hard-killed after the
gateway comes up.

Keeping the whole host paused was rejected because the same pause makes gateway
middleware return 503, preventing the parent's readiness proof from ever
passing. Deferring only the restarter keeps the gateway observable while agents
remain quiesced.

## Update: every launch path enforces the boundary

The fresh start now also classifies itself from a one-shot parent marker or the
executing deploy lease. It leaves host posture `converging` and omits the
restarter; a concurrent operator start fails before migrations. Recovery starts
use the same transient restarter skip, so rollback cannot relaunch agents while
the gateway is still returning.

The boundary is enforced again at the durable-intent seams. Local unpause does
not respawn a restarter listed in `$AVA_HOME/disabled_services`, and an already
spawned restarter checks that marker before schema or DB startup. If the marker
cannot be read, both paths leave the restarter down. The watchdog's
`PauseController` already blocks the complete roster for both `paused` and
`converging`, so no healthcheck path is allowed to race the orchestrator.

A target revision cannot rewrite the rollback implementation already executing
inside an older parent. The version carrying this boundary is therefore the
compatibility stage: its first child detects the old parent's executing lease,
keeps the restarter down, and defers the credential transition. Once that
revision is the installed parent, later rollouts carry the explicit v1 handoff
and both their forward and recovery paths use these rules.

## Update: the boundary belongs to the gateway child

The cluster-wide executing lease is also visible to pure agent-runners during
Phase B. Treating every internal start under that lease as the gateway's child
left each runner `converging` with its restarter down, exactly the state Phase B
polls to completion. The classification is therefore capability-aware:
operator starts are still refused before migrations on every role, but only a
gateway-capable internal child inherits the parent's delayed-resume boundary.
A pure runner finishes its local update with posture `idle` and its restarter
running.

## Update: predecessor recovery remains a deployment boundary

The compatibility target can control its fresh child, but it cannot replace
functions already imported in the predecessor parent that is conducting that
first rollout. If the compatibility rollout itself fails or receives SIGINT
after stopping services, the predecessor's recovery start still predates the
restarter skip, its unpause still starts the restarter unconditionally, and its
restarter has no durable-disable self-check. Agents can therefore relaunch
before the recovered gateway is ready, and operator restarter intent can be
lost.

This is not presented as fixed retroactively. The boundary revision must be
deployed alone as a monitored compatibility stage; only after it is the
installed parent may a later rollout rely on the versioned handoff and durable
recovery rules. The first stage defers the credential split, limiting its work
to installing that parent, but interruption of the predecessor itself remains
an operator-supervised risk.

## Update: recovery does not trust the partially changed owner password

The credential transition mutates Postgres before Redis and the unit env. If a
later phase fails, the surviving parent may still hold the old owner password.
Failed-update schema rollback now uses the gateway's passwordless local
Postgres admin socket and targets the cluster database explicitly; normal
operator rollback continues to use the runtime owner connection. A fault test
injects failure after the Postgres mutation and requires local-admin rollback,
git reset, dependency sync, and last-known-good start to complete in order.
