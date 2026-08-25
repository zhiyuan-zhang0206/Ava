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
