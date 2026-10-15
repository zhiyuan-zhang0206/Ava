from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

from .support import INPUT_HARNESS, between, helper_source, input_bodies

_NEEDS_SWIFT = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None, reason="needs Swift on macOS"
)


def _run(tmp_path: Path, name: str, checks: str) -> str:
    source = helper_source()
    program = (
        INPUT_HARNESS
        + input_bodies(source)
        + between(source, "/// A bounded straight-line", "/// Post a single key")
        + checks
    )
    path = tmp_path / f"{name}.swift"
    path.write_text(program)
    result = run_bounded(["swift", str(path)], timeout=60, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[:400]
    return result.stdout.strip()


@_NEEDS_SWIFT
def test_native_drag_sequence_validation_and_preallocation(tmp_path: Path) -> None:
    checks = r"""
let input: [String: Any] = ["start_x": -20, "start_y": 40, "end_x": 100, "end_y": 160]
let result = try drag(input)
precondition(posted.count == 15)
precondition(posted[0].type == .mouseMoved && posted[1].type == .leftMouseDown)
precondition(posted.last!.type == .leftMouseUp)
for (index, move) in posted[2..<14].enumerated() {
    precondition(move.type == .leftMouseDragged)
    precondition(abs(move.location.x - (-20 + CGFloat(index + 1) * 10)) < 1e-10)
    precondition(abs(move.location.y - (40 + CGFloat(index + 1) * 10)) < 1e-10)
}
precondition(posted.last!.location == CGPoint(x: 100, y: 160))
precondition(posted.allSatisfy { $0.flags.isEmpty && $0.button == .left })
precondition((result["start"] as? [String: Double])?["x"] == -20)
precondition(result["button"] as? String == "left")
for failure in 1...15 {
    reset(); failAt = failure
    do { _ = try drag(input); fatalError("allocation failure accepted") }
    catch OpError.bad(let message) { precondition(message == "could not create drag mouse event") }
    precondition(posted.isEmpty)
}
reset()
for key in ["start_x", "start_y", "end_x", "end_y"] {
    for invalid: Any in [true, "12", NSNull(), Double.nan, Double.infinity] {
        var bad = input; bad[key] = invalid
        reset()
        do { _ = try drag(bad); fatalError("invalid input accepted") }
        catch OpError.bad(let message) { precondition(message == "drag needs finite numeric \(key)") }
        precondition(posted.isEmpty && created == 0)
    }
    var missing = input; missing.removeValue(forKey: key)
    do { _ = try drag(missing); fatalError("missing input accepted") }
    catch OpError.bad { }
}
for fields: [String: Any] in [["button": "purple"], ["button": 1], ["modifiers": ["super"]],
                              ["modifiers": ["shift", "shift"]], ["modifiers": "cmd"],
                              ["modifiers": ["shift", "ctrl", "alt", "cmd", "shift"]]] {
    var bad = input; bad.merge(fields) { $1 }
    reset()
    do { _ = try drag(bad); fatalError("invalid option accepted") } catch OpError.bad { }
    precondition(posted.isEmpty && created == 0)
}
print("drag contract passed")
"""
    assert _run(tmp_path, "drag", checks) == "drag contract passed"


@_NEEDS_SWIFT
def test_native_drag_stopped_in_the_corner_releases_where_the_pointer_is(tmp_path: Path) -> None:
    checks = r"""
let input: [String: Any] = ["start_x": -20, "start_y": 40, "end_x": 100, "end_y": 160]
let away = CGPoint(x: 300, y: 300)
let corner = CGPoint(x: 0, y: 0)
func kinds() -> [CGEventType] { posted.map { $0.type } }

// The parked pointer is seen before the fifth movement: four movements went out, the
// remaining eight never do, and the button comes up under the pointer, not at the target.
reset(); cursorScript = [away, away, away, away, corner]
do { _ = try drag(input); fatalError("drag not stopped") } catch is UserAbort { }
precondition(kinds() == [.mouseMoved, .leftMouseDown] + Array(repeating: .leftMouseDragged, count: 4)
    + [.leftMouseUp])
precondition(posted.last!.location == CGPoint(x: 0, y: 0))

// Parked before the first movement: the press is still balanced by a release.
reset(); cursorScript = [corner]
do { _ = try drag(input); fatalError("drag not stopped") } catch is UserAbort { }
precondition(kinds() == [.mouseMoved, .leftMouseDown, .leftMouseUp])
precondition(posted.last!.location == CGPoint(x: 0, y: 0))

// An unreadable cursor mid-drag surfaces the failure and releases at the last point sent.
reset(); cursorFailAt = 3
do { _ = try drag(input); fatalError("read failure hidden") }
catch OpError.bad(let message) { precondition(message.contains("corner fail-safe")) }
precondition(kinds() == [.mouseMoved, .leftMouseDown, .leftMouseDragged, .leftMouseDragged, .leftMouseUp])
precondition(abs(posted.last!.location.x) < 1e-9 && abs(posted.last!.location.y - 60) < 1e-9)

// Not parked: the drag completes exactly as before.
reset(); _ = try drag(input)
precondition(posted.count == 15 && posted.last!.location.x == 100)
print("drag abort passed")
"""
    assert _run(tmp_path, "drag_abort", checks) == "drag abort passed"


@_NEEDS_SWIFT
def test_native_drag_button_and_modifiers_are_held_throughout_and_released_in_reverse(
    tmp_path: Path,
) -> None:
    checks = r"""
let base: [String: Any] = ["start_x": 10, "start_y": 20, "end_x": 130, "end_y": 80]
let shiftCmd: CGEventFlags = [.maskShift, .maskCommand]
func kinds() -> [CGEventType] { posted.map { $0.type } }

// Right button with shift and cmd: both modifiers down before the press, held for every
// event of the drag, and released after the button, in the reverse of the order they went down.
var request = base; request["button"] = "right"; request["modifiers"] = ["shift", "cmd"]
var result = try drag(request)
precondition(kinds() == [.flagsChanged, .flagsChanged, .mouseMoved, .rightMouseDown]
    + Array(repeating: .rightMouseDragged, count: 12)
    + [.rightMouseUp, .flagsChanged, .flagsChanged])
precondition(posted[0].code == 56 && posted[1].code == 55, "shift then cmd go down")
precondition(posted[posted.count - 2].code == 55 && posted.last!.code == 56, "cmd then shift come up")
for event in posted[2..<(posted.count - 2)] {
    precondition(event.flags == shiftCmd && event.button == .right, "held modifiers on every mouse event")
}
precondition(posted.last!.flags.isEmpty)
precondition(result["button"] as? String == "right")

// Middle button uses the other-button event kinds.
reset(); request = base; request["button"] = "middle"
_ = try drag(request)
precondition(kinds() == [.mouseMoved, .otherMouseDown] + Array(repeating: .otherMouseDragged, count: 12)
    + [.otherMouseUp])
precondition(posted.allSatisfy { $0.button == .center })

// No modifiers, left button: nothing but the mouse events.
reset(); _ = try drag(base)
precondition(!kinds().contains(.flagsChanged) && posted.allSatisfy { $0.flags.isEmpty })

// An allocation failure anywhere, modifier events included, posts nothing at all.
reset(); request = base; request["modifiers"] = ["shift", "ctrl", "alt", "cmd"]
_ = try drag(request)
let total = created
precondition(kinds().filter { $0 == .flagsChanged }.count == 8)
for failure in 1...total {
    reset(); failAt = failure
    do { _ = try drag(request); fatalError("allocation failure accepted") } catch OpError.bad { }
    precondition(posted.isEmpty, "allocation \(failure) left events posted")
}

// Stopped in the corner mid-drag: the button comes up under the parked pointer, then the
// modifiers come up in reverse order, and nothing else is posted.
reset(); request = base; request["button"] = "right"; request["modifiers"] = ["shift", "cmd"]
cursorScript = [CGPoint(x: 300, y: 300), CGPoint(x: 300, y: 300), CGPoint(x: 0, y: 0)]
do { _ = try drag(request); fatalError("drag not stopped") } catch is UserAbort { }
precondition(kinds() == [.flagsChanged, .flagsChanged, .mouseMoved, .rightMouseDown,
    .rightMouseDragged, .rightMouseDragged, .rightMouseUp, .flagsChanged, .flagsChanged])
precondition(posted[6].location == CGPoint(x: 0, y: 0))
precondition(posted[7].code == 55 && posted[8].code == 56 && posted.last!.flags.isEmpty)

// An unreadable pointer mid-drag releases in the same order, at the last point sent.
reset(); cursorFailAt = 2
do { _ = try drag(request); fatalError("read failure hidden") }
catch OpError.bad(let message) { precondition(message.contains("corner fail-safe")) }
precondition(kinds() == [.flagsChanged, .flagsChanged, .mouseMoved, .rightMouseDown,
    .rightMouseDragged, .rightMouseUp, .flagsChanged, .flagsChanged])
precondition(posted[6].code == 55 && posted[7].code == 56)
print("drag options passed")
"""
    assert _run(tmp_path, "drag_options", checks) == "drag options passed"
