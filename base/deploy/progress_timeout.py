"""The deploy timeout family — one definition of "this host stopped making
progress".

`NO_PROGRESS_TIMEOUT_S` is that single definition. Its consumers share it rather
than calibrate their own: the ops daemon's wedge bound (`services.agent_runner.agent_ops.health`);
the schedule-stall alert (`base.deploy.timing`) is ordered above it. Two clocks
that disagree about "stopped making progress" are two chances to declare a host
dead while it is working, or alive while it is not.

## Why 360 s

The only runtime consumer is the ops daemon's wedge bound, so the value bounds
how long one `/ops` request may legitimately run. Measured on the production
`api_idempotency` ledger (the only record of ops request wall time: `completed_at -
created_at`, 2026-09-11..10-11): 109 completed `lifecycle` requests with p50 1.3 s,
p99 39.2 s, max 94.1 s, and 52 `spawn-launch-v2` requests with max 2.5 s. The longest
deadline any caller puts on an ops request is 130 s (the upload delivery worker), so
an op running past that has already been abandoned by its caller.

360 s is 3.8x the measured maximum and 2.8x the longest caller deadline. It must stay
above ten times `GATEWAY_PREFLIGHT_BUDGET_S` and below the schedule-stall alert; both
orderings are declared and checked in `base.deploy.timing`.

The module also holds the agent-lease family, the `ava start` readiness
clocks and a remote unit's capability bundle lifetime; each constant's comment
names its consumer.
"""

from __future__ import annotations

# The one definition of "this host has stopped making progress" (module
# docstring names its consumers).
NO_PROGRESS_TIMEOUT_S = 360.0

# How often the agent host renews its hosted agents' leases: the one ownership
# beat of `services/agent_runner/agent_host/daemon.py` (`_beat_forever`), which sleeps exactly
# this long between renewals and uses this constant as its step, so the lattice
# checks the beat that actually runs.
AGENT_LEASE_RENEW_INTERVAL_S = 15.0

# The hosted agent ownership lease: `agents_meta.lease_expires_at`.
# Forty renewal beats (the lattice floor is ten), so transient DB renewal
# failures do not immediately relinquish a live turn. Expiry bounds crash
# recovery.
AGENT_LEASE_TTL_S = 600.0

# How long a crash-marked idling row may sit dead before the agent_host beat's
# corpse reaper stamps it terminated ('reaper'). Deliberately longer than
# AGENT_LEASE_TTL_S so a corpse first decays offline (renew skips marked rows)
# and then terminates — the user sees an honest sequence. Also longer than the
# claim-park window after a fatal abort (~4 min observed on the 5858 corpse),
# so a still-running parked row is never raced.
CORPSE_REAP_GRACE_S = 900.0

# How long a pure agent-runner's start preflight keeps re-dialing the gateway
# before it declines (`cli.commands._repo.probe_gateway_or_die`, run by
# `ava start`): whether a *single* refused dial is enough to declare the gateway
# down.
#
# 30 s, because the hole it must survive is one gateway restart — measured at
# ~9 s on the 2026-08-01 rollout (`gateway.log` silent 00:14:45 -> 00:14:54). It
# is deliberately far below `NO_PROGRESS_TIMEOUT_S`, so spending it can never be
# what makes a host look stalled, and it is spent only on the failing path — a
# reachable gateway answers on the first dial.
#
# It is NOT sized to outlast a gateway that really died: that needs the
# watchdog's 60 s round plus a respawn
# (`docs/decisions/runtime/processes/health/2026-07-30-accept-readiness-gate-residual-race.md` priced waiting
# for it here and refused it).
GATEWAY_PREFLIGHT_BUDGET_S = 30.0


# How long `ava start` waits for the services it just launched to pass their
# liveness probes before it reports them unready and exits
# `SERVICES_NOT_READY_EXIT_CODE` (`cli.commands.lifecycle.start`; the root glue applies the
# same critical-tier bound).
#
# Nothing healthy waits: the poll returns the instant every probe passes, and a
# service whose session has died ends the wait at once instead of spending the
# bound. The bound is spent only by a daemon that is alive and has bound nothing,
# and 3 minutes of that is a hung daemon, not a slow one. The one hard datum
# behind the magnitude: on the 2026-07-29 05:09 rollout an agent-runner still took
# `[Errno 61] Connection refused` from a restarting gateway ~39 s in, so "one
# local uvicorn binds its port" is well above 15 s.
#
# Local readiness and the off-box rollout probe check different boundaries.
# The operation deadline must include both; start never waives its verdict.
SERVICE_READY_TIMEOUT_S = 180.0

# The public serving path shares one critical readiness/startup tier across
# CLI readiness and root monitoring. All other services use the shorter tier.
CRITICAL_SERVICE_SESSIONS = frozenset({"gate", "gateway", "frontend", "agent-host", "im-bridge"})

# Initial startup grace for optional services in the root health monitor.
# CLI startup does not wait for optional capabilities: it reports their
# current availability once the core is ready.
NON_CRITICAL_SERVICE_READY_TIMEOUT_S = 45.0

# Startup grace the root health monitor falls back to for a unit that was given no tier above.
# Production assigns every requested unit one of the two tiers, so this is the value of an assembly
# that supplies none. No timer reads it against another: it only delays the first restart of a unit
# whose generation has not yet answered a probe.
DEFAULT_STARTUP_GRACE_S = 20.0

# How long a unit capability bundle (`ava cluster db-authority issue-unit`,
# `base.host.db_authority.unit.issue_bundle`) stays installable by default
# (`--ttl-hours`): a day covers the trip to one unit. The most it may ask for is
# `base.host.db_authority.unit.UNIT_BUNDLE_MAX_TTL_S`.
UNIT_BUNDLE_TTL_S = 24 * 3600.0
