---
type: doc
title: Pause recovery — stranded pauses, gateway reachability evidence, and the pause-scoped healthcheck exemption
description: How a paused host decides whether its pause still has an owner, why the age clock is paused_at not updated_at, when a live deploy lease reads as dead evidence, and why the gateway healthcheck still runs under a pause-scoped block (respawn gated on the owner determination).
tags: []
---

# Pause recovery — stranded pauses, gateway reachability evidence, and the pause-scoped healthcheck exemption

Owned by `ops/controllers/stranded_pause.py` (the pause controller) with two
consumers in `services/healthchecks/gateway.py`. The incident this closes is
issue #2101: a dead update chain left the gateway host paused, and the gateway
stayed DOWN 2h6m — the owner check never ran across 122 blocked watchdog rounds,
and even a running check would have deferred to a live lease whose holder's
gateway was provably unreachable.

## The pause-age anchor is `paused_at`, not `updated_at`

`host_deploy_state.updated_at` is bumped by EVERY transition inside the pause
window — updater-lease renewals included. The 2026-09-10 gateway-watchdog log
shows the shape: 122 rounds of "host paused (posture), skipping tick" and zero
"paused for Xs but ..." lines, because the pause kept reading fresh while its
owner was dead. `paused_at` is the pause window's own anchor (set when the
posture enters `paused`, preserved through `converging`, cleared on `idle`), so
the stranded-pause age is measured from it; rows from before the column existed
fall back to `updated_at`.

## Gateway reachability evidence

The gateway-capability watchdog maintains a host-local down-since marker
(`$AVA_HOME/run/gateway-down-since`, atomic write) every round via
`record_gateway_reachability`: reachable clears it, the FIRST down round stamps
it and later probes keep the anchor, so the marker measures the continuous
outage. A pure agent-runner never records it — a runner cannot tell a gateway
outage from a partition, so it keeps the conservative lease-owns reading.

A live executing deploy lease whose gateway has been unreachable for more than
`GATEWAY_DOWN_OWNER_GRACE_S` (600s) is dead evidence: every leg of a cluster
update runs on the gateway host and needs its gateway, so the holder cannot be
executing anything. The lease then does not own the pause and the local-session
check still decides (consulted, and still authoritative). The 600s bound matches
the manager's "no legitimate rollout holds this host longer than this" alarm
judgment; a rollout's own restart leg keeps the gateway down for roughly a
minute or three.

## Pause-scoped exemption of the gateway healthcheck

A pause-scoped `BlockScope.ALL` still runs the gateway healthcheck on the
gateway capability (`services/watchdog/daemon.py:_checks_for_round`): a gateway
that stays down is the one failure that blocks the rollout's own recovery, which
is worse than probing under a paused host. The respawn itself is gated —
`run_keepalive(respawn_gate=...)` asks the stranded-pause controller's own owner
determination (reachability evidence included) and declines while the pause
still has a live owner, so a live rollout's restart leg is never raced. A
declined round resets the keepalive's failure count and breaker: a decline is
"not yet allowed", never "a respawn cannot cure it".

## Off-pin converge-back

A dead rollout can leave the gateway host off-pin (checkout on the target, pin
un-advanced, migrations unapplied); `assert_schema_current` refuses code ahead
of the DB, so respawning that tree only burns backoff rounds. The gateway
healthcheck's `_restart` then converges the checkout back to the pinned commit
first (`git checkout --detach <pin> --force` under the source-switch marker) —
the pinned binary is the only one whose schema matches. Guards skip the converge
while any orchestration session, live deploy lease, or source switch is in
flight; the cluster pin is never written and no update mechanism runs.
