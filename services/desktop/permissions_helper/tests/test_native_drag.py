"""Execute the native drag body against inert CGEvent substitutes, never a desktop."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

from .. import lifecycle

_NEEDS_SWIFT = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None, reason="needs Swift on macOS"
)

_HARNESS = r"""
import Foundation
import CoreGraphics
import CoreFoundation
enum OpError: Error { case bad(String) }
enum CGEventType: String { case mouseMoved, leftMouseDown, leftMouseDragged, leftMouseUp }
enum MouseButton { case left }
enum Tap { case cghidEventTap }
struct CGEventFlags: OptionSet { let rawValue: Int }
var posted: [[String: Any]] = []
var created = 0
var failAt = 0
var reads = 0
var cursorFailAt = 0
var cursorScript: [CGPoint] = []
var cursorDefault = CGPoint(x: 500, y: 500)
func CGMainDisplayID() -> CGDirectDisplayID { 1 }
func CGDisplayBounds(_ id: CGDirectDisplayID) -> CGRect {
    CGRect(origin: .zero, size: CGSize(width: 1000, height: 800))
}
class CGEvent {
    let kind: CGEventType
    var location: CGPoint
    var flags: CGEventFlags = []
    init?(mouseEventSource: Any?, mouseType: CGEventType,
          mouseCursorPosition: CGPoint, mouseButton: MouseButton) {
        created += 1
        if created == failAt { return nil }
        kind = mouseType
        location = mouseCursorPosition
    }
    init?(source: Any?) {
        reads += 1
        if reads == cursorFailAt { return nil }
        kind = .mouseMoved
        location = cursorScript.isEmpty ? cursorDefault : cursorScript.removeFirst()
    }
    func post(tap: Tap) {
        precondition(flags.isEmpty)
        posted.append(["kind": kind.rawValue, "x": Double(location.x), "y": Double(location.y)])
    }
}
func reset() {
    posted = []; created = 0; failAt = 0
    reads = 0; cursorFailAt = 0; cursorScript = []; cursorDefault = CGPoint(x: 500, y: 500)
}
"""


def _failsafe(source: str) -> str:
    start = "// MARK: - Corner fail-safe"
    return start + source.split(start, 1)[1].split("/// Post a synthetic mouse click", 1)[0]


def _drag(source: str) -> str:
    return "func drag(" + source.split("func drag(", 1)[1].split("/// Post a single key", 1)[0]


def _run(tmp_path: Path, name: str, program: str) -> str:
    path = tmp_path / f"{name}.swift"
    path.write_text(program)
    result = run_bounded(["swift", str(path)], timeout=60, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@_NEEDS_SWIFT
def test_native_drag_sequence_validation_and_preallocation(tmp_path: Path) -> None:
    source = lifecycle._SOURCE.read_text()
    checks = r"""
let input: [String: Any] = ["start_x": -20, "start_y": 40, "end_x": 100, "end_y": 160]
let result = try drag(input)
precondition(posted.count == 15)
precondition(posted[0]["kind"] as? String == "mouseMoved")
precondition(posted[1]["kind"] as? String == "leftMouseDown")
precondition(posted.last!["kind"] as? String == "leftMouseUp")
for (index, move) in posted[2..<14].enumerated() {
    precondition(move["kind"] as? String == "leftMouseDragged")
    precondition(abs((move["x"] as! Double) - (-20 + Double(index + 1) * 10)) < 1e-10)
    precondition(abs((move["y"] as! Double) - (40 + Double(index + 1) * 10)) < 1e-10)
}
precondition(posted.last!["x"] as? Double == 100)
precondition(posted.last!["y"] as? Double == 160)
precondition((result["start"] as? [String: Double])?["x"] == -20)
for failure in 1...15 {
    posted = []; created = 0; failAt = failure
    do { _ = try drag(input); fatalError("allocation failure accepted") }
    catch OpError.bad(let message) { precondition(message == "could not create drag mouse event") }
    precondition(posted.isEmpty)
}
failAt = 0
for key in ["start_x", "start_y", "end_x", "end_y"] {
    for invalid: Any in [true, "12", NSNull(), Double.nan, Double.infinity] {
        var bad = input; bad[key] = invalid
        posted = []; created = 0
        do { _ = try drag(bad); fatalError("invalid input accepted") }
        catch OpError.bad(let message) { precondition(message == "drag needs finite numeric \(key)") }
        precondition(posted.isEmpty && created == 0)
    }
    var missing = input; missing.removeValue(forKey: key)
    do { _ = try drag(missing); fatalError("missing input accepted") }
    catch OpError.bad { }
}
print("drag contract passed")
"""
    output = _run(tmp_path, "drag", _HARNESS + _failsafe(source) + _drag(source) + checks)
    assert output == "drag contract passed"


@_NEEDS_SWIFT
def test_native_drag_stopped_in_the_corner_releases_where_the_pointer_is(tmp_path: Path) -> None:
    source = lifecycle._SOURCE.read_text()
    checks = r"""
let input: [String: Any] = ["start_x": -20, "start_y": 40, "end_x": 100, "end_y": 160]
let away = CGPoint(x: 300, y: 300)
let corner = CGPoint(x: 0, y: 0)

// The parked pointer is seen before the fifth movement: four movements went out, the
// remaining eight never do, and the button comes up under the pointer, not at the target.
reset(); cursorScript = [away, away, away, away, corner]
do { _ = try drag(input); fatalError("drag not stopped") } catch is UserAbort { }
precondition(posted.map { $0["kind"] as! String } == ["mouseMoved", "leftMouseDown"]
    + Array(repeating: "leftMouseDragged", count: 4) + ["leftMouseUp"])
let release = posted.last!
precondition(release["x"] as? Double == 0 && release["y"] as? Double == 0)
precondition(!(release["x"] as? Double == 100 && release["y"] as? Double == 160))

// Parked before the first movement: the press is still balanced by a release.
reset(); cursorScript = [corner]
do { _ = try drag(input); fatalError("drag not stopped") } catch is UserAbort { }
precondition(posted.map { $0["kind"] as! String } == ["mouseMoved", "leftMouseDown", "leftMouseUp"])
precondition(posted.last!["x"] as? Double == 0 && posted.last!["y"] as? Double == 0)

// An unreadable cursor mid-drag surfaces the failure and releases at the last point sent.
reset(); cursorFailAt = 3
do { _ = try drag(input); fatalError("read failure hidden") }
catch OpError.bad(let message) { precondition(message.contains("corner fail-safe")) }
precondition(posted.map { $0["kind"] as! String } == ["mouseMoved", "leftMouseDown",
    "leftMouseDragged", "leftMouseDragged", "leftMouseUp"])
precondition(abs(posted.last!["x"] as! Double) < 1e-9 && abs((posted.last!["y"] as! Double) - 60) < 1e-9)

// Not parked: the drag completes exactly as before.
reset(); _ = try drag(input)
precondition(posted.count == 15 && posted.last!["x"] as? Double == 100)
print("drag abort passed")
"""
    output = _run(tmp_path, "drag_abort", _HARNESS + _failsafe(source) + _drag(source) + checks)
    assert output == "drag abort passed"
