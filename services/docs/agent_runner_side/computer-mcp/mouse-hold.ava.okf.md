---
type: doc
title: "Computer-mcp — mouse hold"
description: "mouse_down and mouse_up keep one button pressed across calls; how a stuck button is prevented and how the model learns of an automatic release."
tags:
- services
- computer-use
---

# Mouse hold (`mouse_down` / `mouse_up`)

`drag` presses and releases inside one call. `mouse_down` / `mouse_up` split that
across calls, for a slider with pauses, press-and-hold or a path of several moves; the
caller composes waits and loops in execute_code. That is state that outlives a call,
so the helper owns it and every way it ends is covered.
The client calls are in `base/host/permissions_helper/client_stateful.py`, with the clipboard's.

## Contract
- `mouse_down(x, y, button="left", modifiers=[], frame=None)`: `x`/`y` both or neither
  (neither presses at the pointer; a frame needs them). `modifiers` come down first and
  stay down. `mouse_up(x, y, button="left", frame=None)` takes no modifiers: they belong
  to the press. Strict input: unknown field, bad button or modifier, half a position all
  fail before any event. Both are Accessibility-gated, stoppable by the corner fail-safe
  ([[native-input.ava.okf.md]]) and need `ping.mouse_hold_v1`, otherwise the client
  refuses. `mouse_down` requests the capture badge for the window it lands on
  ([[observation-frames.ava.okf.md]]); `mouse_up` does not.
- One button at a time (`HeldMouse` in `helper/main.swift`, guarded by one lock). A
  second `mouse_down`, or a `mouse_up` of a button that is not held, is an error with no
  event posted; the latter names the button actually held, or why the helper released it.
  While held: `move` and `scroll` post that button's dragged events (left, right or
  other kind, so the target app sees a drag) carrying the held modifiers; `click` and
  `drag` are refused; `key` and `type` still work.
- `mouse_up(x, y)` drags to the point first, then releases there; without a position it
  releases at the live pointer (the last point Ava put it at if unreadable). The press's
  release and modifier events are allocated at the press, so a release never fails for
  want of an event.

## Ways a hold ends
1. `mouse_up`. The button comes up, then the modifiers in reverse order.
2. The stop gesture. Any fail-safe refusal in `dispatch` releases at the parked pointer;
   a monitor timer (100 ms, alive only while a button is held) notices a parked pointer
   with no call at all, stops the badge and releases. An unreadable pointer never ends a
   hold by itself.
3. Idle bound: 30 s (`heldIdleTimeoutSeconds`) with no `mouse_down`, `move` or `scroll`;
   released at the live pointer, one stderr line.
4. Daemon cleanup: `mouse_release_all` (quiet, never refused) after every
   `release_control`, also a refused one from the agent that holds the button, and before a
   different agent's first action when the screen changed hands. The daemon records the
   holder (`_mouse_holder`) and only then talks to the helper. A failed release is logged
   at error level, returned to the caller as `mouse_release_error`, audited as an error,
   and retried by the next call. The daemon runs no timer for lease expiry: a button
   held past a lease with nobody taking the screen ends at (3). Shutdown makes no helper
   call (a blocking call would break the loop-liveness bound).

Cases 2 and 3 leave a notice. The next mouse reply (`click`, `drag`, `move`, `scroll`,
`mouse_down`, `mouse_up`) carries `auto_released: [{button, reason, x, y, held_seconds,
message}]` once; a `mouse_up` refused for that reason says so in its error.

## Audit and limits
`computer_action` coords are `x,y,button` (`pointer,button` without a position). A helper
that dies while a button is down cannot release it; a physical click clears it. Whether
real apps treat the synthetic drag and the release as a hand would is not established by
the harnesses, which use inert event substitutes: it needs a real desktop.
