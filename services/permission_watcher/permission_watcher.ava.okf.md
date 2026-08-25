---
type: doc
title: macOS Permission Watcher
description: Host-global launchd service that correlates TCC and Application Firewall prompt logs, persists pending incidents, and records their lifecycle in its local log.
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
thread; reader threads only parse their stream and enqueue events. TCC records
retain the responsible application as the subject and, when different, the
requesting binary as the tool.

A new incident is persisted and logged at INFO. Repeated observations refresh its
last-seen time and correlation ID but log only at DEBUG. A matching result removes
the incident and logs at INFO, while an incident still pending after 30 minutes
logs one WARNING. Pending state, including whether that warning has fired, is
atomically persisted in an owner-readable JSON file so launchd restarts preserve
the incident lifecycle.

## Delivery and lifecycle

The watcher is a pure detector. It does not write `agent_notices` or call any
delivery channel. The 2026-08-25 user ruling established that macOS permission
popups are system events and must not consume an agent's notice slot. Delivery
through the alerts channel is pending a separate integration.

`cli/commands/_converge_permission_watcher.py` installs
`com.ava.permission-watcher` as a gateway-scoped, host-global LaunchAgent with
`RunAtLoad` and `KeepAlive`. launchd owns restart and log capture, so this process
does not appear in the `ServiceSpec` roster or watchdog checks. An unchanged
plist is a strict converge no-op.

## Dependencies

- [[cli/commands/commands.ava.okf.md]] — launchd convergence
