# Permission watcher delivery boundary

## Context

A recurring TCC prompt attributed to the uv Python runtime reappeared every
20–50 minutes and produced 32 pairs of P1 agent notices overnight. The prompt is
a machine-level permission event, but direct writes to `agent_notices` bound its
lifecycle to an individual agent's notice slot.

## Decision

macOS permission popups are system events and must not be delivered through
`agent_notices`. The permission watcher becomes a pure detector: it correlates
TCC and ALF records, persists local pending state, and logs new and resolved
incidents plus one warning after 30 minutes. Repeated observations refresh the
same pending incident and remain DEBUG-only.

User-facing delivery will move to the alerts channel in a separate change. The
current change deliberately adds no replacement delivery path.

## Consequences

The watcher no longer depends on the database or IM bridge, and a permission
incident cannot occupy or spam an agent notice slot. Until alerts integration is
implemented, operators inspect the unchanged launchd log path and local state
JSON for permission activity.

Update: alert delivery was added later the same day; see
[`permission-watcher-alert-delivery.md`](permission-watcher-alert-delivery.md).
