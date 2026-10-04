---
type: doc
title: Alert Classes
description: The sidebar Warnings / Errors card — the active alert-class count, the grouped class list it opens, and per-class dismiss / reopen.
tags:
- frontend
---

# Alert Classes

The Warnings / Errors card in the stats popover (`agent-sidebar/footer.tsx`) shows the selected window's **active alert-class count** (`alert_classes_active` of `GET /api/stats/dashboard`); the raw event total is secondary text. The card is the toggle of an inline list (`agent-sidebar/alert-classes.tsx`, data hooks in `lib/alert-classes.ts`) — inline, not a dialog, because a dialog opened from inside the popover unmounts with it.

- A class is `(level, event_name, source, process)`. Rows come from `GET /api/stats/alert-classes`, most frequent first: level, event name, source and process, `×count`, first and last seen. A truncated list says how many classes it cut.
- A row opens to its newest events (`/api/stats/alert-classes/samples`, read only when opened) and a Dismiss / Reopen button. Dismiss posts the class's own identity (its `category` included) to `/api/event-resolutions`; Reopen posts the row's `dismissal_id` to `/api/event-resolutions/{id}/reopen`. A failed action shows its reason on the row.
- Dismissed classes sit apart under a collapsed heading. Every action invalidates the `["stats"]` query family, so the card number and the list move together.

The backend contract: [[gateway/cluster/docs/ops-surfaces.ava.okf.md|Ops Surfaces]].
