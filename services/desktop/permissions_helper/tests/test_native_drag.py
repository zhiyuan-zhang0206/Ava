"""Execute the native drag body against inert CGEvent substitutes, never a desktop."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None, reason="needs Swift on macOS"
)
def test_native_drag_sequence_validation_and_preallocation(tmp_path: Path) -> None:
    source = (Path(__file__).parents[1] / "helper/main.swift").read_text()
    body = "func drag(" + source.split("func drag(", 1)[1].split("/// Post a single key", 1)[0]
    harness = r"""
import Foundation
import CoreFoundation
enum OpError: Error { case bad(String) }
enum CGEventType: String { case mouseMoved, leftMouseDown, leftMouseDragged, leftMouseUp }
enum MouseButton { case left }
enum Tap { case cghidEventTap }
struct CGEventFlags: OptionSet { let rawValue: Int }
var posted: [[String: Any]] = []
var created = 0
var failAt = 0
class CGEvent {
    let kind: CGEventType
    let point: CGPoint
    var flags: CGEventFlags = []
    init?(mouseEventSource: Any?, mouseType: CGEventType,
          mouseCursorPosition: CGPoint, mouseButton: MouseButton) {
        created += 1
        if created == failAt { return nil }
        kind = mouseType
        point = mouseCursorPosition
    }
    func post(tap: Tap) {
        precondition(flags.isEmpty)
        posted.append(["kind": kind.rawValue, "x": point.x, "y": point.y])
    }
}
"""
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
    program = tmp_path / "drag.swift"
    program.write_text(harness + body + checks)
    result = run_bounded(["swift", str(program)], timeout=60, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "drag contract passed"
