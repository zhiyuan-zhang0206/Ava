"""Execute actual native input bodies with inert CGEvent and clock substitutes."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

from .. import lifecycle


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None, reason="needs Swift on macOS"
)
def test_native_input_events_validation_and_release(tmp_path: Path) -> None:
    source = lifecycle._SOURCE.read_text()
    helpers = (
        "func inputNumber("
        + source.split("func inputNumber(", 1)[1].split("/// Post a synthetic mouse click", 1)[0]
    )
    click = (
        "func click(" + source.split("func click(", 1)[1].split("/// A bounded straight-line", 1)[0]
    )
    actions = "func key(" + source.split("func key(", 1)[1].split("/// Report the geometry", 1)[0]
    dispatch = (
        "func dispatch(" + source.split("func dispatch(", 1)[1].split("// MARK: - Panel mode", 1)[0]
    )
    stubs = """
var trusted = true
func axTrustedOrPrompt() -> Bool { trusted }
func AXIsProcessTrusted() -> Bool { trusted }
func CGPreflightScreenCaptureAccess() -> Bool { false }
let axGrantError = "Accessibility grant missing"
struct RootSeed { static func from(_ raw: [String: Any]) throws -> RootSeed { RootSeed() } }
class Keeper {
    func configure(_ seed: RootSeed) throws { }
    func status() -> [String: Any] { [:] }
    func requestStop() throws -> [String: Any] { [:] }
}
let rootKeeper = Keeper()
func requestHelperShutdown(runDir: String) throws -> [String: Any] { [:] }
"""
    for name in [
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
    ]:
        stubs += f"func {name}(_ req: [String: Any]) throws -> [String: Any] {{ [:] }}\n"
    for name in ["listApps", "sessionInfo", "frontmostApp"]:
        stubs += f"func {name}() -> [String: Any] {{ [:] }}\n"
    harness = r"""
import Foundation
import CoreGraphics
import CoreFoundation
enum OpError: Error { case bad(String) }
enum Thread {
    static var waits: [Double] = []
    static func sleep(forTimeInterval value: Double) { waits.append(value) }
}
var posted: [CGEvent] = []
var created = 0
var failAt = 0
class CGEvent {
    var type: CGEventType = .null
    var flags: CGEventFlags = []
    var timestamp: UInt64 = 0
    var location: CGPoint = CGPoint(x: -8, y: 9)
    var button: CGMouseButton = .left
    var code: CGKeyCode = 0
    var clickState: Int64 = 0
    var wheels: [Int32] = []
    init?() {
        created += 1
        if created == failAt { return nil }
    }
    convenience init?(mouseEventSource: Any?, mouseType: CGEventType,
                     mouseCursorPosition: CGPoint, mouseButton: CGMouseButton) {
        self.init(); type = mouseType; location = mouseCursorPosition; button = mouseButton
    }
    convenience init?(keyboardEventSource: Any?, virtualKey: CGKeyCode, keyDown: Bool) {
        self.init(); type = keyDown ? .keyDown : .keyUp; code = virtualKey
    }
    convenience init?(scrollWheelEvent2Source: Any?, units: CGScrollEventUnit, wheelCount: UInt32,
                      wheel1: Int32, wheel2: Int32, wheel3: Int32) {
        self.init(); type = .scrollWheel; wheels = [wheel1, wheel2, wheel3]
        precondition(wheelCount == 2 && units == .pixel)
    }
    convenience init?(source: Any?) { self.init() }
    func setIntegerValueField(_ field: CGEventField, value: Int64) { clickState = value }
    func post(tap: CGEventTapLocation) { posted.append(self) }
}
func reset() { posted = []; created = 0; failAt = 0; Thread.waits = [] }
"""
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
precondition(Thread.waits == [0.025, 0.075, 0.025, 0.075, 0.025])
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
precondition(Thread.waits == [0.1])
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
trusted = true; reset()
precondition(dispatch(["id": 1, "method": "click", "x": 1, "y": 2, "button": "right"])["ok"] as? Bool == true)
precondition(posted.map { $0.type } == [.mouseMoved, .rightMouseDown, .rightMouseUp])
print("native input contract passed")
"""
    program = tmp_path / "input.swift"
    program.write_text(harness + helpers + click + actions + stubs + dispatch + checks)
    result = run_bounded(["swift", str(program)], timeout=60, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native input contract passed"
