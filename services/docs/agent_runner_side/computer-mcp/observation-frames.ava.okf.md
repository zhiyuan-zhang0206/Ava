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
An `AppTarget` selects exactly one `pid` or `bundle_id` for explicit AX
observation or foreground activation. Native activation must reject missing or ambiguous apps
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

## Capture badge on the operated window

While an agent operates a window, macOS can show its own "this window is being
captured" badge on it. The system draws it only for a live ScreenCaptureKit
stream, not for a single screenshot, so the permissions helper keeps a minimal
stream on the window an input lands on; Ava draws nothing itself. The helper
decides the window from facts it can read at the moment of the input, and shows
nothing when it cannot establish one.

- Window decision (`indicatorWindow` in `helper/main.swift`), from
  `CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements])`
  in its front-to-back order, in the same global coordinates as input:
  - `click`, `move`, `scroll`: the first window whose bounds contain the point;
    `drag`: the same for its start point; `mouse_down`: its position, or the pointer's when it names none (a half-given position marks nothing; `mouse_up` marks nothing). If that topmost window is not layer 0
    (a menu, popup or system UI), the click lands on it, so nothing is marked and
    the ordinary window behind it is not.
  - `type`, `key`: the frontmost application's PID (after `refreshAppKit`, so the
    answer is not a stale cache) and that PID's frontmost layer-0 on-screen window.
  - `ax_act`: inside `axAct`, after its stale checks and before the action, the
    element's window frame (position and size of the walk's window element) is
    matched, to 1 point, against the layer-0 windows of the already pinned PID;
    exactly one match marks it, otherwise nothing.
  - `focus_app` produces no input and marks nothing. No private API is used.
- Timing: `dispatch` decides and requests the badge after the corner fail-safe and
  before the input is posted, so a stopped action marks nothing. Requesting never
  waits: the lease is recorded under a lock and the ScreenCaptureKit window query
  and `startCapture` run on a private serial queue through their completion
  handlers. The badge cannot delay or fail an input; a failure (no Screen
  Recording grant, window gone) is one stderr line.
- Lease: one 64x64, 1 fps, no-cursor `desktopIndependentWindow` stream at most,
  frames dropped on a private queue, held for 10 s per input. The same window only
  extends the expiry, never shortens it; another window stops the old stream
  first. The helper ends the lease on a private-queue timer, so a daemon that dies
  leaves the badge for at most 10 s. Each lease has a generation: a window query or
  start that finishes after its lease was replaced, stopped or expired is discarded
  and its stream stopped at once. A stream the system ends clears the lease.
- Stop: the corner fail-safe stops the stream before it reports the refusal. The
  daemon calls `indicator_stop` (served only with `ping.capture_indicator_v1`) after
  every successful `release_control`; it is a no-op without a stream. The client
  refuses it on a helper without the capability, and the daemon logs a warning.
  There is no wire method that asks the helper to show a badge.
- Failure semantics, an explicit exception to fail-fast because the badge is
  display-only: a failure to show or to stop it is logged and never fails or
  changes the outcome of an action or a release.
- Harnesses exercise the window decisions and the lease with inert system
  substitutes. Whether macOS actually draws the badge, and that ScreenCaptureKit
  completions arrive while the helper's accept loop is blocked, need a real
  Screen Recording grant and are checked by hand.
