---
type: doc
title: macOS Permission Watcher
description: Host-global launchd service that correlates TCC and Application Firewall prompt logs, persists pending incidents, and sends deduplicated FYI notices through Ava's IM delivery path.
tags:
- service
- gateway
- macos
- permissions
---

# macOS Permission Watcher

## Responsibility

`services/permission_watcher/events.py` observes and correlates machine-wide TCC
and Application Firewall log records without GUI automation. `watcher.py` owns
incidents keyed by permission kind and subject. The state owner is a single main
thread; reader threads only parse their stream and enqueue events.

A new incident produces one P1 FYI notice. Repeated observations of the same
subject are coalesced in a rolling five-minute window. A matching result produces
a resolved notice, while an incident still pending after 30 minutes produces one
escalation. Pending state is atomically persisted in an owner-readable JSON file
so launchd restarts preserve the incident lifecycle. State is written before the
first notice attempt; an unnotified persisted incident is retried after restart,
giving pending delivery at-least-once behavior across transient database failure.

## Delivery and lifecycle

`notices.py` reads `AVA_DB_URL` from the prod home's `.env` and inserts notices
for machine-monitor agent 312 through the same `agent_notices` contract consumed
by the IM bridge. The watcher never calls a channel API directly; IM remains the
only delivery frontend.

`cli/commands/_converge_permission_watcher.py` installs
`com.ava.permission-watcher` as a gateway-scoped, host-global LaunchAgent with
`RunAtLoad` and `KeepAlive`. launchd owns restart and log capture, so this process
does not appear in the `ServiceSpec` roster or watchdog checks. An unchanged
plist is a strict converge no-op.

## Dependencies

- [[services/gateway_side/gateway_side.ava.okf.md]] — capability ownership
- [[services/gateway_side/im_bridge.ava.okf.md]] — notice delivery frontend
- [[cli/commands/commands.ava.okf.md]] — launchd convergence
