---
type: doc
title: "Computer-mcp — native input contracts"
description: "Atomic mouse buttons, click counts, modifier chords, key holds, horizontal scrolling and actual cursor observations."
tags:
- services
- computer-use
---

# Native input contracts

`services/desktop/computer/input.py` owns strict action inputs and derives the
MCP schemas. Invalid booleans, strings, nonfinite coordinates, unknown modifiers,
unknown buttons, duplicate modifiers and out-of-range values fail before input
is posted. Negative global coordinates remain valid for multiple displays.
`permissions_helper/wire.py` owns Python result shapes; `client.py` retains its
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

Extended options on existing methods require ping.native_input_v1 from the
helper. An older running helper fails explicitly before a right button or
modifier request can be silently ignored. New method names already fail when
unknown. This repository change does not restart or deploy the helper.

There is no cross-call held-input state or background input backend. A caller
composes repeats and waits through execute_code. An operator screen release
changes lease ownership; it does not interrupt a synchronous native press.
Global Escape cancellation and target-scoped background event delivery remain
unsupported. Native source harnesses exercise inert event substitutes; a
successful compile or socket test does not establish real desktop delivery.
