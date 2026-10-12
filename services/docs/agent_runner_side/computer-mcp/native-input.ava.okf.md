---
type: doc
title: "Computer-mcp — native input contracts"
description: "Atomic mouse buttons, click counts, modifier chords, key holds, horizontal scrolling, actual cursor observations and the top-left-corner stop gesture."
tags:
- services
- computer-use
---

# Native input contracts

`services/desktop/computer/input.py` owns strict action inputs and derives the
MCP schemas. Invalid booleans, strings, nonfinite coordinates, unknown modifiers,
unknown buttons, duplicate modifiers and out-of-range values fail before input
is posted. Negative global coordinates remain valid for multiple displays.
`base/host/permissions_helper/wire.py` owns Python result shapes; `client.py` retains its
existing explicit type exports for consumers.

`click(x, y, button="left", click_count=1, modifiers=[], duration_ms=0)` accepts
left/right/middle buttons and one to three clicks. The legacy `double=true`
means two clicks; supplying a conflicting click_count fails. Each press may be
held for 0..5000 ms, with a 75 ms gap between clicks. Modifier names are
`shift`, `ctrl`, `alt` and `cmd`, held through the whole sequence.

`key(key=... | keycode=..., modifiers=[], duration_ms=0, cmd=false)` accepts
exactly one supported name or an integer macOS virtual keycode in 0..65535.
Modifier key names themselves are supported. A key may be held atomically for
0..10000 ms. The legacy cmd flag is combined with the explicit modifiers.
The helper preallocates every event before pressing, then releases the key and
modifiers with local defer blocks. Click also releases every mouse press before
returning. These bounded actions fit the existing helper RPC timeout.

`move(x, y, modifiers=[])` moves the pointer without clicking. `cursor_position()`
reads the actual native cursor and returns physical x/y using the daemon's
current measured scale, rather than the last synthetic click. `scroll(dx=0,
dy=0, modifiers=[], x=..., y=...)` requires dx or dy, each a signed Int32 pixel
delta. Explicit positions require both x and y; otherwise scroll reads the live
native cursor in logical points and does not divide it again.

Positions use the existing screenshot pixel coordinate system by default.
Click, move, scroll with explicit coordinates and both drag endpoints accept an
explicit region snapshot `frame`, converting local pixels with its origin and
scale. They do not replace whole-screen scale or tracked pointer state. Window
frames explicitly refuse global pointer input: [[observation-frames.ava.okf.md]].

The clipboard tools are in [[clipboard.ava.okf.md]].

Extended options on existing methods require ping.native_input_v1 from the
helper. An older running helper fails explicitly before a right button or
modifier request can be silently ignored. New method names already fail when
unknown. This repository change does not restart or deploy the helper.

## Corner fail-safe (user stop gesture)

User instruction: move the mouse to the top-left corner of the main display and
keep it there until Ava stops. This is a best-effort sampled check in the helper
(`cornerFailsafe` in `helper/main.swift`), not a keyboard listener, cancellation
token or resident poller; it runs only while a desktop action is executing, or
while Ava holds a mouse button across calls.

- Trigger: the live pointer (`CGEvent(source: nil).location`, CG global
  coordinates, origin top-left) within 1 point of the origin of
  `CGDisplayBounds(CGMainDisplayID())`. It is the real corner, not
  `x <= 0 && y <= 0`, which a display up-left of the main one would turn into a
  whole quadrant. Only the main display's corner counts; there is no hot-zone
  size or dwell time. The pointer is not attributed, so an agent move onto the
  corner also trips it.
- Checked: before `click`, `drag`, `move`, `focus_app`, `type`, `key`, `scroll`,
  `clipboard_write`, `mouse_down`, `mouse_up` and `ax_act` (in `dispatch`, after the Accessibility grant gate); inside
  `drag` before each of its 12 movements; and in 20 ms slices of every click or
  key hold and of the gap between clicks. Not checked: observation (`snapshot`,
  `cursor_position`, `screen_size`, `ax_tree`, `ax_window_info`, `list_*`,
  `window_info`, `frontmost_app`, `session_info`, `clipboard_read`, `ping`), so the model can still
  look at the screen while the pointer is parked.
- Cleanup bypasses the check. A stopped action still posts the mouse-up,
  key-up and modifier releases it pressed (modifiers last, in reverse order of
  pressing), and nothing else. A stopped drag or
  long press releases the button where the pointer is parked, not at the planned
  drag end or click point (that would drop the item or complete the click, and
  move the user's pointer). If the pointer cannot be read mid-drag, the release
  is at the last point sent.
- Result: the helper reply is `{"ok": false, "error": ..., "error_code":
  "user_abort"}`; `client.parse_reply` raises `UserAbortError` (a
  `PermissionsHelperError`), and the daemon returns it as an ordinary failed
  tool call (`UserAbortError: Stopped by the user ...`) audited with outcome
  `error`. Nothing retries it or falls back, and nothing is replayed after the
  mouse moves away: the next request is checked against the pointer at that
  time. While the pointer stays in the corner every later desktop-changing call
  fails the same way. An unreadable pointer is a different, plain error. A button
  held across calls is released by the stop too: [[mouse-hold.ava.okf.md]].
- Limits: events already handed to the system, an AX action already in flight
  and a quick swipe through the corner between samples cannot be recalled or
  seen; a drag competes with the human hand, so the pointer must be held there
  for a moment. The helper advertises `ping.corner_failsafe_v1`; a helper built
  before it has no fail-safe, and Python does not refuse its actions.

The only cross-call held input is one mouse button ([[mouse-hold.ava.okf.md]]); there
is no background input backend. A caller composes repeats and waits through
execute_code. An operator screen release changes lease ownership and lets go of a
held button; it does not interrupt a synchronous native press.
Global Escape cancellation and target-scoped background event delivery remain
unsupported. Native source harnesses exercise inert event substitutes; a
successful compile or socket test does not establish real desktop delivery.
