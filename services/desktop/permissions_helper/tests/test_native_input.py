from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

from .support import INPUT_HARNESS, between, dispatch_stubs, helper_source, input_bodies


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None, reason="needs Swift on macOS"
)
def test_native_input_events_validation_and_release(tmp_path: Path) -> None:
    source = helper_source()
    helpers = input_bodies(source)
    click = between(source, "func click(", "/// A bounded straight-line")
    actions = between(source, "func key(", "/// Report the geometry")
    dispatch = between(source, "func dispatch(", "// MARK: - Panel mode")
    stubs = dispatch_stubs(
        [
            "fileList",
            "fileRead",
            "listWindows",
            "focusApp",
            "screencaptureWindow",
            "screencaptureRegion",
            "drag",
            "typeText",
            "axWindowInfo",
            "axTree",
            "axAct",
            "windowInfo",
            "screenSize",
            "spawnProcess",
            "sessionList",
            "sessionHas",
            "signalSession",
            "indicatorStop",
            "clipboardRead",
            "clipboardWrite",
            "mouseDown",
            "mouseUp",
            "mouseReleaseAll",
        ]
    )
    checks = r"""
let request: [String: Any] = ["x": -10, "y": 20, "button": "right", "click_count": 3,
                             "modifiers": ["shift", "cmd"], "duration_ms": 25]
_ = try click(request)
precondition(posted.map { $0.type } == [.flagsChanged, .flagsChanged, .mouseMoved,
    .rightMouseDown, .rightMouseUp, .rightMouseDown, .rightMouseUp, .rightMouseDown,
    .rightMouseUp, .flagsChanged, .flagsChanged])
precondition(posted[3].clickState == 1 && posted[5].clickState == 2 && posted[7].clickState == 3)
precondition(posted[2].location.x == -10 && posted[2].button == .right)
precondition(posted[3].flags == [.maskShift, .maskCommand] && posted.last!.flags.isEmpty)
// Holds and gaps are slept in short slices so the corner fail-safe can interrupt them.
precondition(abs(Thread.waits.reduce(0, +) - 0.225) < 1e-9)
precondition(Thread.waits.allSatisfy { $0 <= failsafeSliceSeconds + 1e-12 })
let allocations = created
for failure in 1...allocations {
    reset(); failAt = failure
    do { _ = try click(request); fatalError("allocation accepted") } catch OpError.bad { }
    precondition(posted.isEmpty)
}
reset(); _ = try click(["x": 1, "y": 2, "button": "middle"])
precondition(posted.map { $0.type } == [.mouseMoved, .otherMouseDown, .otherMouseUp])
reset(); _ = try key(["code": 0, "modifiers": ["ctrl", "alt"], "duration_ms": 100])
precondition(posted.map { $0.type } == [.flagsChanged, .flagsChanged, .keyDown, .keyUp, .flagsChanged, .flagsChanged])
precondition(posted[2].flags == [.maskControl, .maskAlternate] && posted.last!.flags.isEmpty)
precondition(abs(Thread.waits.reduce(0, +) - 0.1) < 1e-9)
precondition(Thread.waits.allSatisfy { $0 <= failsafeSliceSeconds + 1e-12 })
let keyAllocations = created
for failure in 1...keyAllocations {
    reset(); failAt = failure
    do { _ = try key(["code": 0, "cmd": true]); fatalError("allocation accepted") } catch OpError.bad { }
    precondition(posted.isEmpty)
    if failure == 4 { break }
}
reset(); _ = try key(["code": 56, "duration_ms": 50])
precondition(posted.map { $0.type } == [.flagsChanged, .flagsChanged])
precondition(posted[0].flags == .maskShift && posted[1].flags.isEmpty)
reset(); _ = try scroll(["x": 2, "y": 3, "dx": 9, "dy": -7, "modifiers": ["alt"]])
precondition(posted.map { $0.type } == [.flagsChanged, .mouseMoved, .scrollWheel, .flagsChanged])
precondition(posted[2].wheels == [-7, 9, 0] && posted.last!.flags.isEmpty)
reset(); _ = try mouseMove(["x": -2, "y": 5])
precondition(posted.count == 1 && posted[0].type == .mouseMoved)
let cursor = try cursorPosition()
precondition(cursor["x"] as? CGFloat == -8)
for bad: Any in [true, "1", NSNull(), Double.nan, Double.infinity] {
    reset()
    do { _ = try click(["x": bad, "y": 1]); fatalError("invalid coordinate accepted") } catch OpError.bad { }
    precondition(posted.isEmpty && created == 0)
}
for bad: Any in [true, "1", 1.5, -1, 65536] {
    reset()
    do { _ = try key(["code": bad]); fatalError("invalid code accepted") } catch OpError.bad { }
    precondition(posted.isEmpty && created == 0)
}
for fields: [String: Any] in [["button": "unknown"], ["click_count": 0], ["click_count": true],
    ["duration_ms": -1], ["duration_ms": 5001], ["duration_ms": true], ["double": 1],
    ["modifiers": ["super"]], ["modifiers": ["shift", "shift"]], ["modifiers": "cmd"]] {
    reset(); var bad: [String: Any] = ["x": 1, "y": 2]; bad.merge(fields) { $1 }
    do { _ = try click(bad); fatalError("invalid click accepted") } catch OpError.bad { }
    precondition(posted.isEmpty && created == 0)
}
reset(); trusted = false
for method in ["click", "drag", "move", "focus_app", "type", "key", "scroll", "ax_tree", "ax_act"] {
    let response = dispatch(["id": 1, "method": method, "x": 1, "y": 2, "code": 0])
    precondition(response["ok"] as? Bool == false)
    precondition(response["error"] as? String == axGrantError)
    precondition(posted.isEmpty && created == 0)
}
precondition(dispatch(["id": 1, "method": "cursor_position"])["ok"] as? Bool == true)
// The clipboard is not synthetic input: it needs no Accessibility grant.
stubCalls = 0
for method in ["clipboard_read", "clipboard_write"] {
    precondition(dispatch(["id": 1, "method": method])["ok"] as? Bool == true, "\(method) gated")
}
precondition(stubCalls == 2)
trusted = true; reset()
precondition(dispatch(["id": 1, "method": "click", "x": 1, "y": 2, "button": "right"])["ok"] as? Bool == true)
precondition(posted.map { $0.type } == [.mouseMoved, .rightMouseDown, .rightMouseUp])
func parkedAt(_ x: Double, _ y: Double) -> Bool {
    cursorScript = [CGPoint(x: x, y: y)]
    do { try cornerFailsafe(); return false } catch is UserAbort { return true } catch { fatalError("\(error)") }
}
reset()
precondition(parkedAt(0, 0) && parkedAt(0.5, 0.9))
precondition(!parkedAt(1, 0) && !parkedAt(0, 1) && !parkedAt(500, 400))
// A secondary display up-left of the main one must not trip the quadrant.
precondition(!parkedAt(-1, -1) && !parkedAt(-400, -300) && !parkedAt(-1, 0))
mainOrigin = CGPoint(x: 10, y: 20)
precondition(parkedAt(10, 20) && !parkedAt(0, 0))
mainOrigin = .zero
cursorReadFails = true
do { try cornerFailsafe(); fatalError("unreadable cursor accepted") }
catch OpError.bad(let message) { precondition(message.contains("corner fail-safe")) }
cursorReadFails = false

// Dispatch: every desktop-changing method is refused in the corner with no side effect.
reset(); cursorDefault = CGPoint(x: 0, y: 0); abortStops = 0; holdCalls = []
for method in ["click", "move", "scroll", "key", "type", "drag", "focus_app", "ax_act", "clipboard_write"] {
    let stopsBefore = abortStops
    let response = dispatch(["id": 1, "method": method, "x": 1, "y": 2, "dy": 3, "code": 0])
    precondition(abortStops == stopsBefore + 1, "\(method) abort left the capture indicator running")
    precondition(response["ok"] as? Bool == false)
    precondition(response["error_code"] as? String == "user_abort")
    precondition((response["error"] as? String)?.contains("top-left corner") == true)
    precondition(posted.isEmpty && created == 0 && stubCalls == 0)
}
// A stopped action shows no badge: the fail-safe runs before the hold.
precondition(holdCalls.isEmpty)
// Observation stays available while the pointer is parked.
for method in ["cursor_position", "screen_size", "ax_tree", "ax_window_info", "list_apps",
               "list_windows", "window_info", "session_info", "frontmost_app", "ping",
               "screencapture_window", "screencapture_region", "clipboard_read"] {
    let before = stubCalls
    let response = dispatch(["id": 1, "method": method])
    precondition(response["ok"] as? Bool == true, "\(method) blocked")
    precondition(response["error_code"] == nil)
    precondition(method == "cursor_position" || method == "ping" || stubCalls == before + 1)
}
precondition(posted.isEmpty)
// The indicator commands are display-only: not stoppable, not Accessibility-gated.
abortStops = 0; trusted = false
let callsBeforeStop = stubCalls
let stopResponse = dispatch(["id": 1, "method": "indicator_stop"])
precondition(stopResponse["ok"] as? Bool == true && stopResponse["error_code"] == nil)
precondition(stubCalls == callsBeforeStop + 1 && abortStops == 0)
// indicator_hold is not a wire method: the helper decides which window to mark itself.
precondition(dispatch(["id": 1, "method": "indicator_hold", "pid": 1, "window_id": 2, "seconds": 3])["ok"] as? Bool == false)
precondition(holdCalls.isEmpty)
trusted = true
// Leaving the corner lifts the refusal; nothing refused earlier is replayed.
cursorDefault = CGPoint(x: 300, y: 300)
precondition(dispatch(["id": 1, "method": "click", "x": 1, "y": 2])["ok"] as? Bool == true)
precondition(posted.map { $0.type } == [.mouseMoved, .leftMouseDown, .leftMouseUp])
// An unreadable cursor is a visible failure, not a silent pass.
reset(); cursorReadFails = true
let unreadable = dispatch(["id": 1, "method": "click", "x": 1, "y": 2])
precondition(unreadable["ok"] as? Bool == false && unreadable["error_code"] == nil)
precondition((unreadable["error"] as? String)?.contains("corner fail-safe") == true)
precondition(posted.isEmpty && created == 0)
let ping = dispatch(["id": 1, "method": "ping"])["result"] as! [String: Any]
precondition(ping["corner_failsafe_v1"] as? Bool == true)
precondition(ping["capture_indicator_v1"] as? Bool == true)
precondition(ping["clipboard_v1"] as? Bool == true)

// Out of the corner, each input method asks for its badge once, before any event is posted.
// Nothing else does: focus_app produces no input, ax_act decides inside its own body, and
// observation changes nothing.
reset(); cursorDefault = CGPoint(x: 300, y: 300); holdCalls = []
for method in ["click", "drag", "move", "scroll", "type", "key"] {
    let before = holdCalls.count
    posted = []
    _ = dispatch(["id": 1, "method": method, "x": 1, "y": 2, "dy": 3, "code": 0])
    precondition(holdCalls.count == before + 1 && holdCalls.last!.method == method, "\(method) badge")
    precondition(holdCalls.last!.postedBefore == 0, "\(method) badge came after input")
}
holdCalls = []
for method in ["focus_app", "ax_act", "ax_tree", "cursor_position", "list_windows", "screencapture_window",
               "indicator_stop", "ping", "clipboard_read", "clipboard_write"] {
    _ = dispatch(["id": 1, "method": method, "x": 1, "y": 2, "dy": 3, "code": 0])
}
precondition(holdCalls.isEmpty)
// Without the Accessibility grant nothing is posted, so nothing is marked.
trusted = false
_ = dispatch(["id": 1, "method": "click", "x": 1, "y": 2])
trusted = true
precondition(holdCalls.isEmpty)

// A long press stopped mid-hold: the button is released where the pointer is parked
// (never back at the click point, which would complete the click), and nothing else posts.
reset(); cursorDefault = CGPoint(x: 300, y: 300)
cursorScript = [CGPoint(x: 300, y: 300), CGPoint(x: 0, y: 0)]
do { _ = try click(["x": 40, "y": 50, "duration_ms": 100, "modifiers": ["shift"]]); fatalError("hold not stopped") }
catch is UserAbort { }
precondition(posted.map { $0.type } == [.flagsChanged, .mouseMoved, .leftMouseDown, .leftMouseUp, .flagsChanged])
precondition(posted[2].location == CGPoint(x: 40, y: 50))
precondition(posted[3].location == CGPoint(x: 0, y: 0))
precondition(posted.last!.flags.isEmpty)
precondition(Thread.waits.count == 2)
// Stopped between two clicks: nothing is held, so no further press is posted.
reset(); cursorScript = [CGPoint(x: 0, y: 0)]
do { _ = try click(["x": 40, "y": 50, "click_count": 2]); fatalError("gap not checked") }
catch is UserAbort { }
precondition(posted.map { $0.type } == [.mouseMoved, .leftMouseDown, .leftMouseUp])
// An unreadable cursor mid-hold still releases at the press point and surfaces the failure.
reset(); cursorFailAt = 2
do { _ = try click(["x": 40, "y": 50, "duration_ms": 100]); fatalError("read failure hidden") }
catch OpError.bad(let message) { precondition(message.contains("corner fail-safe")) }
precondition(posted.map { $0.type } == [.mouseMoved, .leftMouseDown, .leftMouseUp])
precondition(posted[2].location == CGPoint(x: 40, y: 50))
// A key hold stopped mid-hold releases the key and its modifiers.
reset(); cursorScript = [CGPoint(x: 300, y: 300), CGPoint(x: 0, y: 0)]
do { _ = try key(["code": 0, "modifiers": ["ctrl"], "duration_ms": 100]); fatalError("hold not stopped") }
catch is UserAbort { }
precondition(posted.map { $0.type } == [.flagsChanged, .keyDown, .keyUp, .flagsChanged])
precondition(Thread.waits.count == 2 && posted.last!.flags.isEmpty)
// A mid-action stop reaches the dispatch reply path too: the stream is stopped once, before it.
reset(); abortStops = 0; cursorDefault = CGPoint(x: 300, y: 300)
cursorScript = [CGPoint(x: 300, y: 300), CGPoint(x: 300, y: 300), CGPoint(x: 0, y: 0)]
let midAction = dispatch(["id": 1, "method": "click", "x": 40, "y": 50, "duration_ms": 100])
precondition(midAction["error_code"] as? String == "user_abort" && abortStops == 1)
// An ordinary failure is not a stop: the indicator keeps running.
reset(); abortStops = 0; cursorDefault = CGPoint(x: 300, y: 300)
precondition(dispatch(["id": 1, "method": "click", "x": "bad", "y": 2])["ok"] as? Bool == false)
precondition(abortStops == 0)
print("native input contract passed")
"""
    program = tmp_path / "input.swift"
    program.write_text(INPUT_HARNESS + helpers + click + actions + stubs + dispatch + checks)
    result = run_bounded(["swift", str(program)], timeout=60, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native input contract passed"
