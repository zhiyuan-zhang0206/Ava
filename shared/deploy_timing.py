"""The deploy timeout family — one definition of "this host stopped making
progress", and the rule that keeps the deploy lease alive across it.

`NO_PROGRESS_TIMEOUT_S` is that single definition. Its consumers share it rather
than calibrate their own: the settle hold (`shared.cluster_lock.SETTLE_TTL_S`),
the updater lease TTL (`shared.host_deploy_state.UPDATER_LEASE_TTL_S`) and the ops
daemon's wedge bound (`services.agent_ops.health`); the schedule-stall alert
(`shared.timing`) is ordered above it. Two clocks that disagree about
"stopped making progress" are two chances to declare a host dead while it is
working, or alive while it is not: in the 2026-07-29 incident the smallest of
three independent bounds decided when the deploy lease stopped protecting the
deploy, and a second deploy could start into a host whose checkout had moved
while its processes had not.

## The invariant

**The lease must not expire before the operation it protects can finish.**

It is held two ways, and the split matters:

1. **While an operation is executing**, the process running it renews the lease
   on a timer (`LEASE_RENEW_INTERVAL_S`; `shared.cluster_lock.renew_update_lock`,
   driven by `services.pitr.activation_lease`). So `LOCK_TTL_S` is not a ceiling
   on how long a deploy may take; it is purely the crash-reclaim bound, and an
   operation whose process dies still releases within one TTL because renewal
   dies with it.
2. **After the operation stops executing** with hosts still mid-transition, the
   lease converts to a bounded settle hold (`SETTLE_TTL_S`) that
   `ops.deploy_window` ends the moment those hosts converge.

## Why 900 s

The value is far above the measured POSIX agent-runner leg (2-15 s across 44
samples on two hosts, 2026-07-01..29) because the Windows leg is two orders of
magnitude slower: production rollouts of 2026-08-06..12 converged `win` inside
0-11 minutes, and five rounds spent the whole bound. It is roughly 1.5x the
longest leg observed, not a first-principles ceiling; what has to stay true is
the invariant above, which `shared.timing` checks.

The module also holds the agent-lease family and the `ava start` readiness
clocks; each constant's comment names its consumer.
"""

from __future__ import annotations

# The one definition of "this host has stopped making progress" (module
# docstring names its consumers).
NO_PROGRESS_TIMEOUT_S = 900.0

# How often the process running an orchestration re-arms its own lease. Small
# relative to `LOCK_TTL_S` so a missed round (a slow DB, one dropped connection) is
# never fatal, and large enough that a multi-minute rollout costs a handful of
# single-row UPDATEs rather than a poll-rate write stream.
LEASE_RENEW_INTERVAL_S = 60.0

# The hosted agent ownership lease: `agents_meta.lease_expires_at`.
# TTL = 10x the renewal interval, so transient DB renewal failures do not
# immediately relinquish a live turn. Expiry bounds crash recovery.
AGENT_LEASE_TTL_S = 600.0

# How long a crash-marked idling row may sit dead before the agent_host beat's
# corpse reaper stamps it terminated ('reaper'). Deliberately longer than
# AGENT_LEASE_TTL_S so a corpse first decays offline (renew skips marked rows)
# and then terminates — the user sees an honest sequence. Also longer than the
# claim-park window after a fatal abort (~4 min observed on the 5858 corpse),
# so a still-running parked row is never raced.
CORPSE_REAP_GRACE_S = 900.0
AGENT_LEASE_RENEW_INTERVAL_S = 60.0

# How much renewal silence a legacy (NULL-resource) hosted row must show before
# a same-machine successor may replace its owner without waiting the full
# `AGENT_LEASE_TTL_S` (issue #2156). The hosted ownership beat renews leases
# every 15 s (`services/agent_host/daemon.py` `_LIVENESS_BEAT_STEP_S`), so this
# is four missed beats — the same "the host stopped beating" idiom as the
# turn-progress heartbeat TTL — while staying far inside the lease TTL the
# fence still protects. Silence is only one probe of the evidence set; the
# others (no live same-home host daemon, no live exec child of the agent) are
# gathered in `shared.host_process_evidence`.
LEGACY_HOST_ADOPTION_SILENCE_S = 60.0
# How long a pure agent-runner's start preflight keeps re-dialing the gateway
# before it declines (`cli.commands._repo._probe_gateway_or_die`, run by
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
# (`decisions/2026-07-30-accept-readiness-gate-residual-race.md` priced waiting
# for it here and refused it).
GATEWAY_PREFLIGHT_BUDGET_S = 30.0


# How long `ava start` waits for the services it just launched to pass their
# liveness probes before it reports them unready and exits
# `SERVICES_NOT_READY_EXIT_CODE` (`cli.commands.start`; the root glue applies the
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

# How long `ava start` waits for a NON-CRITICAL service to pass its liveness
# probe before it stops waiting on it (`cli.commands._probe`).
#
# The readiness gate is tiered: the critical roster keeps
# `SERVICE_READY_TIMEOUT_S`, because a start that cannot serve the core
# surface or the ops safety net is a failed start. The roster is the CTO
# ruling (Task #2183, C2): gateway / frontend / restarter / the hosted
# agent-runner / im-bridge / the two watchdogs (see
# `cli.commands._probe.CRITICAL_SERVICE_SESSIONS`). Everything else —
# pitr-uploader, labeler, the browser, ... — shares one short window instead,
# sized so a slow-but-healthy daemon still gets its beat to bind its port
# while a dead one stops taxing every start. 2026-08-30 rollout-1788074072
# spent 182 s of its 197.5 s local start waiting on a pitr-uploader healthz
# that never answered; the service's failure did not block the rollout's
# conclusion (the watchdog covers it), so the gate was waiting on a service
# whose verdict nothing depended on.
#
# Deliberately well below `SERVICE_READY_TIMEOUT_S`: the point of the tier is
# that the short window ends long before the critical bound, so a healthy start
# is never held to the long number by a straggling non-critical daemon. A
# non-critical service that misses the window does NOT fail the start — it is
# reported and posted as an alert instead (see
# `cli.commands._probe._notify_non_critical_unready_services`), so the
# downgrade never goes silent.
NON_CRITICAL_SERVICE_READY_TIMEOUT_S = 45.0
