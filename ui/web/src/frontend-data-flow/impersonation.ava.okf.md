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
