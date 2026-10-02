---
type: doc
title: Impersonation timeline projection
description: Permanent impersonation messages hydrate the existing timeline with executor metadata and scoped numeric cursors.
tags: [frontend, timeline]
---

# Impersonation messages

The normal timeline query hydrates permanent impersonation entries under a
checkpoint session marker. `impersonation_changed` and `inbound_arrived` refresh
that query while the native agent is parked. The same numeric item cursors,
snapshot merge and compact-history paging handle these extra blocks. There is
no separate client message store. Each card carries optional `impersonation`
metadata (agent/session id, name, executor and observed process); user direction
and the Ava agent identity stay intact. The card header does not render
this metadata — no executor/takeover badge (user ruling 2026-09-16, task #3660).
The roster card also carries an optional open session number plus that lease's
phase (`requested` / `accepted` / `active`, or null with no open lease — see
`base/agents/observation/roster.py`'s `open_impersonation` LATERAL join). Only `active`
means the agent is actually taken over: the console projects that case to a
distinct `impersonated` status (replacing `idling`/`running` — see
`projectAgentStatus` in `src/lib/types.ts`), which is the only visible sidebar
difference (user ruling — no separate takeover label or on-row button). The
open session number alone (any open phase) still gates the right-click
context-menu's "End external takeover session" item — the sidebar's only
takeover control. That action confirms and sends the exact session number; the
response reports whether it expired or was no longer open and refetches the
roster. Agent-updated events also refetch the roster when another client opens
or ends a session. No timeline or message marker is added for the control. The
same relative API call and roster read work with shared SSE on HTTPS and
per-page SSE on direct HTTP.

Inspector Liveness Status reads the same selected-agent roster projection:
an active takeover displays `Impersonated` even though `/inspect/live` carries
the parked native lifecycle. Release or roster removal restores the Inspector
lifecycle status; a terminated Inspector response always remains terminated.
This is a reader of the existing roster cache, with no separate lease state.
