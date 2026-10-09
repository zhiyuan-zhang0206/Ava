---
type: doc
title: Computer MCP — Drag
description: The bounded left-button drag action and its coordinate, permission, release and validation contracts.
tags: []
---

`drag(start_x, start_y, end_x, end_y)` performs one left-button straight-line
drag over roughly 120 ms. Both endpoints use physical pixels, including negative
coordinates on displays left or above the primary display. Conversion uses the
same measured scale as `click` (the live helper scale before any capture). The
result echoes `start` and `end` in helper logical points, matching the `click`
result convention. A successful drag tracks its physical endpoint for later
scrolls and audits both endpoints. Missing, nonnumeric, boolean or nonfinite
coordinates fail explicitly. The helper requires Accessibility and prepares all
events before pressing; it releases the button within the synchronous call even
if the requesting socket disconnects.
