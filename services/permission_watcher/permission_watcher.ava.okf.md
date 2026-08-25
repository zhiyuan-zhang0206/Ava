---
type: doc
title: macOS Permission Watcher
description: Host-global launchd service that correlates TCC and Application Firewall prompt logs, persists incident cooldown state, and delivers firing/resolved system alerts through the gateway.
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

A new incident is persisted in `full` mode, logged at INFO, and posted as a
firing alert. Repeated observations while it remains pending refresh its
last-seen time and correlation ID but never post another successful firing;
they log only at DEBUG. If the initial post failed, a later observation retries
the same instance using its original `first_seen` as `startsAt`.

A matching result posts `resolved` only when the firing was delivered, replaying
the same labels and `startsAt` and setting `endsAt` from the result event. The
pending entry is then removed and its resolution time is recorded. A recurrence
within 12 hours is tracked as a `silent` pending incident: neither its firing nor
its resolution is posted. After 12 hours, the next recurrence starts a new full
instance. Pending mode, delivery state, and per-key resolution times are
atomically persisted in an owner-readable version-1 JSON file; resolution times
older than 48 hours are pruned on save, while pending incidents older than 24
hours are dropped on load. There is no timeout or escalation tick.

## Delivery and lifecycle

The watcher posts Alertmanager-webhook-shaped payloads to the loopback gateway
at `/api/alerts` on `settings.gateway.gateway_port` (falling back to port 8000
when settings are unavailable), with
`source=permission-watcher`, `alertname=permission-prompt`, and
`severity=warning`. Labels use permission kind plus the responsible application
subject; the variable triggering tool appears only in the Chinese summary so it
cannot fragment alert identity. The alerts ingest owns persistence, UI/SSE
visibility, and IM fan-out.

The poster reads `AVA_OPS_ALERTS_WEBHOOK_TOKEN` from the prod home's `.env`
(with the process settings as fallback), sends it in `X-Alerts-Token`, and
retries one failed HTTP attempt after a short backoff. A second failure is
logged and dropped. There is no direct-database fallback and no
`agent_notices` write: the 2026-08-25 user ruling classifies permission popups
as system events, not agent-owned notices.

`cli/commands/_converge_permission_watcher.py` installs
`com.ava.permission-watcher` as a gateway-scoped, host-global LaunchAgent with
`RunAtLoad` and `KeepAlive`. launchd owns restart and log capture, so this process
does not appear in the `ServiceSpec` roster or watchdog checks. An unchanged
plist is a strict converge no-op.

## Dependencies

- [[cli/commands/commands.ava.okf.md]] — launchd convergence
- [[gateway/routers/alerts.ava.okf.md]] — alert ingest, storage, SSE, and IM fan-out
