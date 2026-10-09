---
type: doc
title: "Computer-mcp — explicit observation frames"
description: "Stateless window identities, logical region capture and screenshot-local pixel coordinate validation."
tags:
- services
- computer-use
---

# Explicit observation frames

`services/desktop/computer/targets.py` owns stateless observation selectors
and coordinate validation. A `WindowTarget` pairs `pid` with `window_id`;
native operations must resolve both against the live window server rather
than substituting the frontmost app. An app selector is an exact display name
or bundle identifier; an explicitly empty or malformed selector is an error.
An `AppTarget` for explicit foreground activation instead selects exactly one
`pid` or `bundle_id`. Native activation must reject missing or ambiguous apps
and confirm the requested process became frontmost. It does not select a
particular window; callers must capture a fresh observation after activation.

`CaptureFrame` describes screenshot-local pixels with `coordinate_space`,
`origin` in global logical points, `scale`, and physical `pixels.width/height`.
`window_pixels` additionally requires a `target` containing the owning `pid`
and `window_id`; `region_pixels` has no window identity. Callers can return
this frame intact with pointer coordinates. The region mapping adds the
logical origin after dividing the local pixels by the measured scale, and
rejects points outside the image. Window-frame pointer input is explicitly
unsupported: a window screenshot does not establish a target-scoped input
backend. Neither frame selects an app persistently or implicitly focuses it.

`screen.capture_region` accepts an integer logical rectangle (`x/y/w/h`) wholly
within the current main display. It preserves negative global origins and
measures backing scale from the PNG, rejecting inconsistent dimensions.
Partial clipping, regions on another display and mixed-display rectangles
are outside this capture contract. A local capture must not replace the
daemon's whole-screen scale or whole-screen OCR cache.
