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
