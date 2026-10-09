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

## Public operations

`snapshot(region={x,y,w,h})` returns `source="region"` and a frame. Its OCR boxes
are local to that capture and do not replace the whole-screen OCR cache.
`snapshot(target={pid,window_id})` returns `source="window"` and a window frame.
Selectors are mutually exclusive. Whole-screen calls retain the existing
screen/pixels result and include_ax behavior; include_ax is refused for local
captures because it would combine different coordinate spaces.

`list_apps()` lists running processes with nullable display name and bundle ID.
`list_windows(app=...)` returns layer-zero, nonempty windows, including small
dialogs and off-screen windows. It requires Screen Recording for metadata and
rejects missing or ambiguous explicit app selectors. `focus_app(target={pid})`
or `focus_app(target={bundle_id})` explicitly activates one running app and
confirms the focused PID through AX. A failed postcondition may follow an
activation side effect; callers must capture again before deciding their next
action. The helper pumps bounded default-run-loop turns for AppKit freshness.

Window captures use macOS 14+ ScreenCaptureKit's desktopIndependentWindow filter,
exclude cursor/shadow pixels, and recheck PID/window ID and geometry after the
capture. Bounded callbacks pump the main run loop instead of blocking it on a
semaphore. Closed, recycled or moving targets fail explicitly. No title or
geometry matching selects an AX window. A caller can explicitly raise a real
AX window root through ax_act perform_action/native_action="AXRaise" only when
that element reports the action, then activate its app and capture again.
Neither operation implies background input support.
