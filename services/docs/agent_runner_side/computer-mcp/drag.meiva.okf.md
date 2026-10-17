---
type: doc
title: Computer MCP — Drag
description: The bounded left-button drag action and its coordinate, permission, release and validation contracts.
tags: []
---

`drag(start_x, start_y, end_x, end_y, button="left", modifiers=[])` performs one
straight-line drag over roughly 120 ms with the left, right or middle button (the
matching dragged event kind, so the target sees the right button dragged).
`modifiers` (`shift`, `ctrl`, `alt`, `cmd`) are pressed before the mouse move, ride on
every event and come up after the button, in reverse order; a stopped drag releases
the button under the parked pointer, then the modifiers. Options need
`ping.mouse_hold_v1` (older helpers would ignore them, so the client refuses) and a
button already held by `mouse_down` makes drag fail
([[mouse-hold.meiva.okf.md]]). Events are posted with posting-time timestamps. Both endpoints use physical pixels, including negative
coordinates on displays left or above the primary display. Conversion uses the
same measured scale as `click` (the live helper scale before any capture). The
result echoes `start` and `end` in helper logical points, matching the `click`
result convention. A successful drag tracks its physical endpoint for later
scrolls and audits both endpoints. Missing, nonnumeric, boolean or nonfinite
coordinates, an unknown button and bad modifiers fail explicitly. The helper requires Accessibility and prepares all
events before pressing; it releases the button within the synchronous call even
if the requesting socket disconnects.
