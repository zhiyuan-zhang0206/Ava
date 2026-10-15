---
type: doc
title: E2E fixture evidence
description: Navigation, browser callbacks and controlled scenario premises.
---

# E2E fixture evidence

`E2EEnv` is immutable fixture data; fixtures still own its page and processes.
Ordinary scenarios navigate through `agent_url`, whose explicit agent ID avoids
racing the initially empty sidebar. Bare `frontend_url` is deliberate when
testing automatic agent selection.

Synchronous Playwright callbacks need its event loop to run. The message-flow
and compact-switch-back tests use `page.evaluate` before inspecting request or
console callbacks; a bare sleep cannot substitute for that pump. Switch-back
uses condition waits and two witnesses, with expanded details pinned so the
compact card exists and compact-history retention disabled so intended retained
history is not mistaken for stale state. The current scenario uses a controlled
compact-history fixture rather than proving a live compact request.

Rendering-sensitive scenarios pin settings through the real settings REST API
before the first navigation, rather than inheriting mutable product defaults.
Details `all` exposes secondary DOM content; zero compact-history retention
isolates clear-on-compact behavior and is not normal retention evidence.

The error-recovery fake sets its independent once-only error flag before raising
`FatalProviderError`. The parent has not consumed a message, so its cursor has
not advanced; using that cursor as the error flag would fail every round.

History-pagination races hold an HTTP response independently, scroll while it
is in flight, then release it. A blocking sleep cannot establish that ordering.
Only the standing-prompt anchor corrects for expected pull-space contraction;
ordinary message anchors keep the raw three-pixel bound. The test polls for a
durable reply before reload to enter the initial tail window, but its timeout
exit does not prove completion or `has_more`. A growing SSE store alone is not
the pagination entrance.
