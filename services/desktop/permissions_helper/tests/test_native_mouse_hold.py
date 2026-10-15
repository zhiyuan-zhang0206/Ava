from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

from .support import (
    INPUT_HARNESS,
    between,
    dispatch_stubs,
    helper_source,
    input_bodies,
)

_NEEDS_SWIFT = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None, reason="needs Swift on macOS"
)

_STUBBED = [
    "fileList",
    "fileRead",
    "listWindows",
    "focusApp",
    "screencaptureWindow",
    "screencaptureRegion",
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
]

_PRELUDE = r"""
func clean() {
    _ = try? mouseReleaseAll([:])
    _ = takeHeldNotices()
    reset(); monitorStarts = 0; monitorStops = 0; abortStops = 0; holdCalls = []
    cursorDefault = CGPoint(x: 300, y: 300); trusted = true
}
func kinds() -> [CGEventType] { posted.map { $0.type } }
func fails(_ body: () throws -> Any) -> String {
    do { _ = try body(); fatalError("accepted") } catch OpError.bad(let message) { return message }
    catch { fatalError("\(error)") }
}
let corner = CGPoint(x: 0, y: 0)
"""


def _run(tmp_path: Path, name: str, checks: str) -> str:
    source = helper_source()
    program = (
        INPUT_HARNESS
        + input_bodies(source)
        + between(source, "func click(", "/// A bounded straight-line")
        + between(source, "/// A bounded straight-line", "/// Post a single key")
        + between(source, "func key(", "/// Report the geometry")
        + dispatch_stubs(_STUBBED)
        + between(source, "func dispatch(", "// MARK: - Panel mode")
        + _PRELUDE
        + checks
    )
    path = tmp_path / f"{name}.swift"
    path.write_text(program)
    result = run_bounded(["swift", str(path)], timeout=90, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[:400]
    return result.stdout.strip()


@_NEEDS_SWIFT
def test_native_mouse_down_up_events_and_pairing(tmp_path: Path) -> None:
    checks = r"""
clean()
// A press at a point with modifiers: modifiers down, move, press. The release is held back.
var result = try mouseDown(["x": -10, "y": 20, "button": "right", "modifiers": ["shift", "cmd"]])
precondition(kinds() == [.flagsChanged, .flagsChanged, .mouseMoved, .rightMouseDown])
precondition(posted[2].location == CGPoint(x: -10, y: 20) && posted[3].clickState == 1)
precondition(posted[3].flags == [.maskShift, .maskCommand] && posted[3].button == .right)
precondition((result["pressed"] as? [String: Double])?["x"] == -10 && result["button"] as? String == "right")
precondition(result["modifiers"] as? [String] == ["shift", "cmd"])
precondition(monitorStarts == 1 && monitorStops == 0)

// The release brings the button up, then the modifiers in reverse order, and ends the watch.
posted = []
result = try mouseUp(["button": "right"])
precondition(kinds() == [.rightMouseUp, .flagsChanged, .flagsChanged])
precondition(posted[0].location == CGPoint(x: 300, y: 300), "no position: released where the pointer is")
precondition(posted[0].flags == [.maskShift, .maskCommand] && posted[0].clickState == 1)
precondition(posted[1].code == 55 && posted[2].code == 56 && posted.last!.flags.isEmpty)
precondition(monitorStops == 1)
precondition((result["released"] as? [String: Double])?["x"] == 300 && result["button"] as? String == "right")
precondition((result["held_ms"] as? Int) != nil)

// Without a position the press lands where the pointer is, and no move is posted.
clean()
_ = try mouseDown([:])
precondition(kinds() == [.leftMouseDown] && posted[0].location == CGPoint(x: 300, y: 300))
_ = try mouseUp([:])
clean()
_ = try mouseDown(["button": "middle"]); _ = try mouseUp(["button": "middle"])
precondition(kinds() == [.otherMouseDown, .otherMouseUp] && posted.allSatisfy { $0.button == .center })

// Releasing at a different point drags there first, as a hand would; at the same point it does not.
clean()
_ = try mouseDown(["x": 5, "y": 5]); posted = []
_ = try mouseUp(["x": 80, "y": 90])
precondition(kinds() == [.leftMouseDragged, .leftMouseUp])
precondition(posted.allSatisfy { $0.location == CGPoint(x: 80, y: 90) })
clean()
_ = try mouseDown(["x": 5, "y": 5]); posted = []
_ = try mouseUp(["x": 5, "y": 5])
precondition(kinds() == [.leftMouseUp])

// One button at a time: a repeated press, or a press of another button, is refused with no event.
clean()
_ = try mouseDown(["x": 1, "y": 2]); posted = []; let starts = monitorStarts
precondition(fails { try mouseDown(["x": 1, "y": 2]) }.contains("already held"))
precondition(fails { try mouseDown(["button": "right"]) }.contains("already held"))
precondition(posted.isEmpty && monitorStarts == starts)
// Releasing a button that is not held is an error that names what is held, and releases nothing.
let wrong = fails { try mouseUp(["button": "right"]) }
precondition(wrong.contains("not held") && wrong.contains("left"))
precondition(posted.isEmpty)
_ = try mouseUp([:])
// Nothing is held now: the release is refused, still without an event.
posted = []
precondition(fails { try mouseUp([:]) }.contains("not held"))
precondition(posted.isEmpty)

// Strict input: unknown fields, half a position, wrong types. Nothing is posted or held.
clean()
for bad: [String: Any] in [["x": 1], ["y": 1], ["x": "1", "y": 2], ["x": true, "y": 2], ["button": "purple"],
    ["button": 3], ["modifiers": ["super"]], ["modifiers": ["shift", "shift"]], ["modifiers": "cmd"],
    ["duration_ms": 5]] {
    posted = []; created = 0
    _ = fails { try mouseDown(bad) }
    precondition(posted.isEmpty && created == 0, "\(bad)")
}
precondition(fails { try mouseUp(["modifiers": ["shift"]]) } == "unknown field: modifiers")
for bad: [String: Any] in [["x": 1], ["button": "purple"], ["x": Double.nan, "y": 1]] {
    _ = fails { try mouseUp(bad) }
}
precondition(monitorStarts == 0)

// An allocation failure at any point leaves nothing posted and nothing held.
clean()
_ = try mouseDown(["x": 1, "y": 2, "modifiers": ["shift", "alt"]]); _ = try mouseUp([:])
let total = created
for failure in 1...total {
    clean(); failAt = failure
    _ = fails { try mouseDown(["x": 1, "y": 2, "modifiers": ["shift", "alt"]]) }
    precondition(posted.isEmpty, "allocation \(failure) left events posted")
    failAt = 0
    precondition(fails { try mouseUp([:]) }.contains("not held"), "allocation \(failure) left a hold")
}
print("press and release passed")
"""
    assert _run(tmp_path, "mouse_hold", checks) == "press and release passed"


@_NEEDS_SWIFT
def test_native_held_button_makes_moves_drags_and_blocks_clicks(tmp_path: Path) -> None:
    checks = r"""
clean()
// Without a hold, move is a plain move.
_ = try mouseMove(["x": 3, "y": 4])
precondition(kinds() == [.mouseMoved])

// With a hold, move is a drag of the held button, carrying the held modifiers.
clean()
_ = try mouseDown(["x": 1, "y": 2, "button": "right", "modifiers": ["ctrl"]]); posted = []
_ = try mouseMove(["x": 30, "y": 40])
precondition(kinds() == [.rightMouseDragged] && posted[0].button == .right)
precondition(posted[0].location == CGPoint(x: 30, y: 40) && posted[0].flags == .maskControl)
// A move with its own modifier presses only that one: the held one is not pressed twice.
posted = []
_ = try mouseMove(["x": 31, "y": 41, "modifiers": ["ctrl", "alt"]])
precondition(kinds() == [.flagsChanged, .rightMouseDragged, .flagsChanged])
precondition(posted[0].code == 58 && posted[2].code == 58)
precondition(posted[1].flags == [.maskControl, .maskAlternate])
// A scroll over a held button moves with a drag too, and keeps the held modifier on the wheel.
posted = []
_ = try scroll(["x": 50, "y": 60, "dy": -3])
precondition(kinds() == [.rightMouseDragged, .scrollWheel] && posted[1].flags == .maskControl)
// The release goes to the last place the pointer was put when no position is given and the
// live pointer is unreadable.
posted = []; cursorReadFails = true
_ = try mouseUp(["button": "right"])
precondition(posted[0].location == CGPoint(x: 50, y: 60))
cursorReadFails = false

// A held button refuses a click and a drag, with no event posted; a key and typing still go.
clean()
_ = try mouseDown(["x": 1, "y": 2]); posted = []
precondition(fails { try click(["x": 1, "y": 2]) }.contains("click refused"))
precondition(fails { try drag(["start_x": 1, "start_y": 2, "end_x": 5, "end_y": 6]) }.contains("drag refused"))
precondition(posted.isEmpty)
_ = try key(["code": 0])
precondition(kinds() == [.keyDown, .keyUp])
_ = try mouseUp([:])
posted = []
_ = try click(["x": 1, "y": 2])
precondition(kinds() == [.mouseMoved, .leftMouseDown, .leftMouseUp])
print("held movement passed")
"""
    assert _run(tmp_path, "mouse_held_moves", checks) == "held movement passed"


@_NEEDS_SWIFT
def test_native_held_button_is_released_by_stop_gesture_idle_bound_and_daemon(
    tmp_path: Path,
) -> None:
    checks = r"""
func press() throws { _ = try mouseDown(["x": 10, "y": 20, "modifiers": ["shift"]]); posted = [] }
func held() -> Bool { (try? requireNoHeldMouse("probe")) == nil }

// Stop gesture seen by an action in dispatch: refused, and the button comes up under the
// parked pointer, then the modifier comes up. The reply is the stop, nothing else is posted.
for method in ["click", "move", "scroll", "key", "type", "drag", "focus_app", "ax_act", "clipboard_write",
               "mouse_down", "mouse_up"] {
    clean(); try press()
    cursorDefault = corner
    let reply = dispatch(["id": 1, "method": method, "x": 1, "y": 2, "dy": 3, "code": 0,
                          "start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4])
    precondition(reply["error_code"] as? String == "user_abort", method)
    precondition(kinds() == [.leftMouseUp, .flagsChanged], "\(method): \(kinds())")
    precondition(posted[0].location == corner && posted[1].code == 56 && posted.last!.flags.isEmpty)
    precondition(monitorStops == 1 && abortStops == 1, "\(method) left the watch or the badge running")
    // Away from the corner the next mouse call is told, once, and nothing is held.
    cursorDefault = CGPoint(x: 300, y: 300); posted = []
    let next = dispatch(["id": 1, "method": "move", "x": 5, "y": 6])
    let notices = (next["result"] as! [String: Any])["auto_released"] as! [[String: Any]]
    precondition(notices.count == 1 && notices[0]["reason"] as? String == "failsafe")
    precondition(notices[0]["button"] as? String == "left" && notices[0]["x"] as? Double == 0)
    precondition((notices[0]["message"] as? String)?.contains("released automatically") == true)
    precondition(kinds() == [.mouseMoved], "after the stop a move is plain")
    let again = (dispatch(["id": 1, "method": "move", "x": 5, "y": 6])["result"] as! [String: Any])
    precondition(again["auto_released"] == nil, "a notice is reported once")
}

// Observation does not stop and does not release.
clean(); try press(); cursorDefault = corner
for method in ["cursor_position", "ping", "list_apps", "screen_size", "frontmost_app", "session_info"] {
    precondition(dispatch(["id": 1, "method": method])["ok"] as? Bool == true, method)
}
precondition(posted.isEmpty && held())
cursorDefault = CGPoint(x: 300, y: 300)

// The monitor sees a parked pointer with no call at all, releases there, and takes the badge down.
clean(); try press(); cursorScript = [corner]
heldMouseTick()
precondition(kinds() == [.leftMouseUp, .flagsChanged] && posted[0].location == corner)
precondition(abortStops == 1 && monitorStops == 1)
let parked = takeHeldNotices()
precondition(parked.count == 1 && parked[0]["reason"] as? String == "failsafe")
// A tick with nothing held, or a pointer elsewhere, or an unreadable one, changes nothing.
clean(); heldMouseTick()
precondition(posted.isEmpty && abortStops == 0)
try press(); heldMouseTick(); cursorReadFails = true; heldMouseTick(now: Date().addingTimeInterval(1))
precondition(posted.isEmpty && held(), "an unreadable pointer must not end the hold")
cursorReadFails = false

// Idle bound: held at 29 s, released at 31 s where the pointer is, with a notice naming it.
clean(); try press()
heldMouseTick(now: Date().addingTimeInterval(heldIdleTimeoutSeconds - 1))
precondition(posted.isEmpty && held())
cursorDefault = CGPoint(x: 77, y: 88)
heldMouseTick(now: Date().addingTimeInterval(heldIdleTimeoutSeconds + 1))
precondition(kinds() == [.leftMouseUp, .flagsChanged] && posted[0].location == CGPoint(x: 77, y: 88))
precondition(abortStops == 0, "an idle release is not a stop")
precondition(monitorStops == 1 && !held())
// The model's next release says why it is refused, and the reason is given once.
let late = fails { try mouseUp([:]) }
precondition(late.contains("not held") && late.contains("released automatically") && late.contains("no mouse call"))
precondition(fails { try mouseUp([:]) }.contains("released automatically") == false)
precondition(takeHeldNotices().isEmpty)

// Moves and scrolls are activity; key presses are not, so a script that keeps moving keeps its hold.
clean(); try press()
func idleSeconds() -> Double { Date().timeIntervalSince(heldMouse!.lastActivity) }
heldMouse!.lastActivity = Date().addingTimeInterval(-25)
_ = try key(["code": 0])
precondition(idleSeconds() > 24, "a key press must not count as mouse activity")
_ = try mouseMove(["x": 11, "y": 21])
precondition(idleSeconds() < 1, "a move keeps the hold")
heldMouse!.lastActivity = Date().addingTimeInterval(-25)
_ = try scroll(["x": 11, "y": 21, "dy": 1])
precondition(idleSeconds() < 1, "a scroll keeps the hold")
heldMouse!.lastActivity = Date().addingTimeInterval(-25)
heldMouseTick(now: Date().addingTimeInterval(10))
precondition(!held(), "idle for 35 s")

// The daemon's release: quiet (no notice), at the live pointer, idempotent, not stoppable.
clean(); try press(); cursorDefault = corner
var released = try mouseReleaseAll([:])
precondition(released["released"] as? [String] == ["left"])
precondition(kinds() == [.leftMouseUp, .flagsChanged] && takeHeldNotices().isEmpty)
released = try mouseReleaseAll([:])
precondition((released["released"] as? [String]) == [])
precondition(fails { try mouseReleaseAll(["x": 1]) } == "unknown field: x")
cursorDefault = CGPoint(x: 300, y: 300)
// It also works with the Accessibility grant gone and the pointer parked (cleanup is never refused).
try press(); trusted = false; cursorDefault = corner
let viaDispatch = dispatch(["id": 1, "method": "mouse_release_all"])
precondition(viaDispatch["ok"] as? Bool == true && (viaDispatch["result"] as! [String: Any])["released"] as? [String] == ["left"])
trusted = true; cursorDefault = CGPoint(x: 300, y: 300)

// Without the grant a press or a release is refused up front; with it, a press asks for its badge
// before any event, a release does not.
clean(); trusted = false
for method in ["mouse_down", "mouse_up"] {
    let refused = dispatch(["id": 1, "method": method, "x": 1, "y": 2])
    precondition(refused["error"] as? String == axGrantError && posted.isEmpty)
}
trusted = true; holdCalls = []
_ = dispatch(["id": 1, "method": "mouse_down", "x": 1, "y": 2])
precondition(holdCalls.count == 1 && holdCalls[0].method == "mouse_down" && holdCalls[0].postedBefore == 0)
_ = dispatch(["id": 1, "method": "mouse_up"])
precondition(holdCalls.count == 1)
precondition((dispatch(["id": 1, "method": "ping"])["result"] as! [String: Any])["mouse_hold_v1"] as? Bool == true)
print("held release passed")
"""
    assert _run(tmp_path, "mouse_hold_release", checks) == "held release passed"
