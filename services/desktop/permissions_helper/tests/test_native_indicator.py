from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

from .. import lifecycle

_HARNESS = r"""
import Foundation
import CoreGraphics
import CoreMedia
enum OpError: Error { case bad(String) }
var screenGranted = true
/// Where the held-button state would read the live pointer (nil: unreadable).
var livePointer: CGPoint? = nil
func liveCursorPosition() -> CGPoint? { livePointer }
func CGPreflightScreenCaptureAccess() -> Bool { screenGranted }
func CGMainDisplayID() -> CGDirectDisplayID { 1 }
func refreshAppKit() { }
class NSRunningApplication {
    let processIdentifier: pid_t
    init(_ pid: pid_t) { processIdentifier = pid }
}
class NSWorkspace {
    static let shared = NSWorkspace()
    var frontmostApplication: NSRunningApplication?
}
var windowRows: [[String: Any]] = []
var lastWindowOptions: CGWindowListOption = []
func CGWindowListCopyWindowInfo(_ option: CGWindowListOption, _ id: CGWindowID) -> CFArray? {
    lastWindowOptions = option
    return windowRows as CFArray
}
func row(_ id: Int, _ pid: Int, layer: Int, _ x: Double, _ y: Double, _ w: Double, _ h: Double) -> [String: Any] {
    [kCGWindowLayer as String: layer, kCGWindowOwnerPID as String: pid, kCGWindowNumber as String: id,
     kCGWindowBounds as String: ["X": x, "Y": y, "Width": w, "Height": h]]
}
class SCApplication { let processID: pid_t; init(_ pid: pid_t) { processID = pid } }
class SCWindow {
    let windowID: UInt32
    let owningApplication: SCApplication?
    init(_ id: UInt32, _ pid: pid_t) { windowID = id; owningApplication = SCApplication(pid) }
}
var desktop: [SCWindow] = [SCWindow(42, 77), SCWindow(43, 77), SCWindow(50, 88)]
var contentBlocked = false
var startBlocked = false
func pause(while blocked: () -> Bool) { while blocked() { Thread.sleep(forTimeInterval: 0.005) } }
class SCShareableContent {
    let windows: [SCWindow]
    init(_ rows: [SCWindow]) { windows = rows }
    static func getExcludingDesktopWindows(_ exclude: Bool, onScreenWindowsOnly: Bool,
                                          completionHandler: @escaping (SCShareableContent?, Error?) -> Void) {
        precondition(exclude && !onScreenWindowsOnly)
        DispatchQueue.global().async {
            pause(while: { contentBlocked })
            completionHandler(SCShareableContent(desktop), nil)
        }
    }
}
class SCContentFilter {
    let window: SCWindow
    init(desktopIndependentWindow: SCWindow) { window = desktopIndependentWindow }
}
class SCStreamConfiguration {
    var width = 0; var height = 0
    var minimumFrameInterval = CMTime.zero
    var showsCursor = true
}
enum SCStreamOutputType { case screen }
protocol SCStreamOutput: NSObjectProtocol {
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType)
}
protocol SCStreamDelegate: NSObjectProtocol {
    func stream(_ stream: SCStream, didStopWithError error: Error)
}
struct StubFailure: Error {}
var startFails = false
var addOutputFails = false
var streams: [SCStream] = []
class SCStream {
    let filter: SCContentFilter
    let configuration: SCStreamConfiguration
    weak var delegate: SCStreamDelegate?
    var outputQueue: DispatchQueue?
    var outputs = 0
    private let guardLock = NSLock()
    private var starts = 0
    private var stops = 0
    private var running = false
    init(filter: SCContentFilter, configuration: SCStreamConfiguration, delegate: SCStreamDelegate?) {
        self.filter = filter; self.configuration = configuration; self.delegate = delegate
        guardLock.lock(); streams.append(self); guardLock.unlock()
    }
    func addStreamOutput(_ output: SCStreamOutput, type: SCStreamOutputType, sampleHandlerQueue: DispatchQueue?) throws {
        if addOutputFails { throw StubFailure() }
        outputs += 1; outputQueue = sampleHandlerQueue
    }
    func startCapture(completionHandler: @escaping (Error?) -> Void) {
        let fails = startFails
        guardLock.lock(); starts += 1; running = !fails; guardLock.unlock()
        DispatchQueue.global().async {
            pause(while: { startBlocked })
            completionHandler(fails ? StubFailure() : nil)
        }
    }
    func stopCapture(completionHandler: @escaping (Error?) -> Void) {
        guardLock.lock(); stops += 1; guardLock.unlock()
        DispatchQueue.global().async { completionHandler(nil) }
    }
    var startCount: Int { guardLock.lock(); defer { guardLock.unlock() }; return starts }
    var stopCount: Int { guardLock.lock(); defer { guardLock.unlock() }; return stops }
    var isLive: Bool { guardLock.lock(); defer { guardLock.unlock() }; return running && stops == 0 }
    /// The system ends the stream on its own (window closed, grant revoked).
    func systemStops() {
        guardLock.lock(); stops += 1; guardLock.unlock()
        delegate?.stream(self, didStopWithError: StubFailure())
    }
}
func liveStreams() -> [SCStream] { streams.filter { $0.isLive } }
func settle(_ seconds: Double) { Thread.sleep(forTimeInterval: seconds) }
func waitUntil(_ what: String, _ condition: () -> Bool) {
    let deadline = Date(timeIntervalSinceNow: 5)
    while !condition() {
        precondition(Date() < deadline, "timed out waiting for \(what)")
        Thread.sleep(forTimeInterval: 0.005)
    }
}
func window(_ pid: Int, _ id: Int) -> IndicatorWindow { IndicatorWindow(pid: pid, windowID: id) }
"""

_CHECKS = r"""
// ---- Which window an input lands on --------------------------------------------------------
// Front to back, as CGWindowListCopyWindowInfo returns them.
windowRows = [
    row(900, 50, layer: 101, 100, 100, 50, 50),     // a popup menu over the windows below
    row(42, 77, layer: 0, 0, 0, 500, 400),
    row(43, 77, layer: 0, 0, 0, 300, 300),          // behind 42
    row(50, 88, layer: 0, 600, 0, 300, 300),
    row(70, 66, layer: 0, 1000, 0, 100, 100),
    row(71, 66, layer: 0, 1000, 0, 100, 100),       // identical frame: not distinguishable
    row(80, 55, layer: 25, 2000, 0, 100, 100),      // a floating panel ahead of the app's normal window
    row(81, 55, layer: 0, 2000, 0, 400, 400),
]
func point(_ method: String, _ x: Double, _ y: Double) -> IndicatorWindow? {
    indicatorWindow(forInput: method, ["x": x, "y": y])
}
precondition(point("click", 10, 10) == window(77, 42))
precondition(lastWindowOptions == [.optionOnScreenOnly, .excludeDesktopElements])
precondition(point("click", 700, 100) == window(88, 50))
// The topmost window takes the click, even when it is not a normal window: nothing is marked.
precondition(point("click", 120, 120) == nil)
precondition(point("click", 2050, 50) == nil)
precondition(point("click", 5000, 5000) == nil)
precondition(point("move", 10, 10) == window(77, 42) && point("scroll", 700, 100) == window(88, 50))
precondition(point("scroll", 120, 120) == nil && point("move", 120, 120) == nil)
// A drag is decided by where it starts.
precondition(indicatorWindow(forInput: "drag", ["start_x": 10, "start_y": 10, "end_x": 700, "end_y": 100]) == window(77, 42))
precondition(indicatorWindow(forInput: "drag", ["start_x": 120, "start_y": 120, "end_x": 10, "end_y": 10]) == nil)
precondition(indicatorWindow(forInput: "drag", ["x": 10, "y": 10]) == nil)
for bad: Any in ["1", true, NSNull(), Double.nan] {
    precondition(indicatorWindow(forInput: "click", ["x": bad, "y": 10]) == nil)
}
precondition(indicatorWindow(forInput: "click", ["y": 10]) == nil)
// A press is decided by its position, or by the pointer when it names none.
precondition(point("mouse_down", 10, 10) == window(77, 42) && point("mouse_down", 700, 100) == window(88, 50))
precondition(point("mouse_down", 120, 120) == nil)
precondition(indicatorWindow(forInput: "mouse_down", [:]) == nil, "unreadable pointer")
livePointer = CGPoint(x: 700, y: 100)
precondition(indicatorWindow(forInput: "mouse_down", ["button": "right"]) == window(88, 50))
livePointer = CGPoint(x: 120, y: 120)
precondition(indicatorWindow(forInput: "mouse_down", [:]) == nil, "a popup on top takes the press")
// A half-given or invalid position is refused by the press itself, so nothing is marked for it.
livePointer = CGPoint(x: 10, y: 10)
precondition(indicatorWindow(forInput: "mouse_down", ["x": 10]) == nil)
precondition(indicatorWindow(forInput: "mouse_down", ["x": "10", "y": 10]) == nil)
// A release lands where the press did and marks nothing of its own.
precondition(indicatorWindow(forInput: "mouse_up", ["x": 10, "y": 10]) == nil)
livePointer = nil
// Keystrokes go to the frontmost app's frontmost normal window.
NSWorkspace.shared.frontmostApplication = NSRunningApplication(77)
precondition(indicatorWindow(forInput: "type", ["text": "x"]) == window(77, 42))
precondition(indicatorWindow(forInput: "key", ["code": 0]) == window(77, 42))
NSWorkspace.shared.frontmostApplication = NSRunningApplication(55)
precondition(indicatorWindow(forInput: "key", ["code": 0]) == window(55, 81))
NSWorkspace.shared.frontmostApplication = NSRunningApplication(50)   // only a popup
precondition(indicatorWindow(forInput: "key", ["code": 0]) == nil)
NSWorkspace.shared.frontmostApplication = NSRunningApplication(12)   // no on-screen window
precondition(indicatorWindow(forInput: "type", ["text": "x"]) == nil)
NSWorkspace.shared.frontmostApplication = nil
precondition(indicatorWindow(forInput: "type", ["text": "x"]) == nil)
// focus_app produces no input and ax_act is decided inside its own body: nothing here.
NSWorkspace.shared.frontmostApplication = NSRunningApplication(77)
for method in ["focus_app", "ax_act", "ax_tree", "ping", "screencapture_window", "list_windows"] {
    precondition(indicatorWindow(forInput: method, ["x": 10, "y": 10, "pid": 77]) == nil, method)
}
// An accessibility window is matched by frame inside the pinned process only.
precondition(indicatorWindow(axFrame: CGRect(x: 0, y: 0, width: 500, height: 400), pid: 77) == window(77, 42))
precondition(indicatorWindow(axFrame: CGRect(x: 0, y: 0, width: 300, height: 300), pid: 77) == window(77, 43))
precondition(indicatorWindow(axFrame: CGRect(x: 0.4, y: -0.4, width: 500.5, height: 399.6), pid: 77) == window(77, 42))
precondition(indicatorWindow(axFrame: CGRect(x: 0, y: 0, width: 500, height: 402), pid: 77) == nil)
precondition(indicatorWindow(axFrame: CGRect(x: 0, y: 0, width: 500, height: 400), pid: 88) == nil)
precondition(indicatorWindow(axFrame: CGRect(x: 1000, y: 0, width: 100, height: 100), pid: 66) == nil)
precondition(indicatorWindow(axFrame: CGRect(x: 100, y: 100, width: 50, height: 50), pid: 50) == nil)

// ---- The stream lease ----------------------------------------------------------------------
func hold(_ pid: Int, _ id: Int, _ seconds: Double) { captureIndicator.hold(window(pid, id), seconds: seconds) }
func stopped() -> Bool { captureIndicator.stop() }

// Stream shape: one minimal stream of the identified window, frames dropped on a private queue.
hold(77, 42, 5)
waitUntil("first stream") { liveStreams().count == 1 }
precondition(streams.count == 1)
let first = streams[0]
precondition(first.filter.window.windowID == 42)
precondition(first.configuration.width == 64 && first.configuration.height == 64)
precondition(first.configuration.minimumFrameInterval == CMTime(value: 1, timescale: 1))
precondition(!first.configuration.showsCursor && first.outputs == 1 && first.outputQueue != nil)

// The same window again only extends: no second stream, and a shorter hold never shortens.
hold(77, 42, 5)
hold(77, 42, 0.01)
settle(0.2)
precondition(streams.count == 1 && first.startCount == 1 && first.isLive, "extension changed the lease")

// Another window: the old stream is stopped, then exactly one new stream runs.
hold(77, 43, 5)
waitUntil("second stream") { streams.count == 2 && streams[1].isLive }
waitUntil("old stream stopped") { first.stopCount == 1 }
precondition(liveStreams().count == 1 && streams[1].filter.window.windowID == 43)

// stop is immediate, idempotent, and harmless without a stream.
var wasStopped = stopped()
precondition(wasStopped && streams[1].stopCount == 1 && liveStreams().isEmpty)
wasStopped = stopped()
precondition(!wasStopped)

// The helper ends an unrenewed lease by itself, and an extension postpones that.
var base = streams.count
hold(77, 42, 0.6)
waitUntil("stream") { streams.count == base + 1 && streams[base].isLive }
settle(0.35)
hold(77, 42, 0.6)
settle(0.35)
precondition(streams[base].isLive, "extension did not postpone expiry")
waitUntil("self-expiry") { streams[base].stopCount == 1 }
precondition(liveStreams().isEmpty)

// The system ending a stream clears the lease: the next hold starts a new stream.
hold(77, 42, 5)
waitUntil("stream") { liveStreams().count == 1 }
let dying = liveStreams()[0]
dying.systemStops()
settle(0.1)
hold(77, 42, 5)
waitUntil("restart") { liveStreams().count == 1 }
precondition(liveStreams()[0] !== dying)
_ = stopped()

// Failures to show the badge leave nothing running and nothing registered.
base = streams.count
startFails = true
hold(77, 42, 5)
waitUntil("failed start") { streams.count == base + 1 }
settle(0.2)
startFails = false
wasStopped = stopped()
precondition(!wasStopped && liveStreams().isEmpty)
addOutputFails = true
hold(77, 42, 5)
settle(0.3)
addOutputFails = false
wasStopped = stopped()
precondition(!wasStopped && liveStreams().isEmpty)
base = streams.count
hold(88, 43, 5)                       // window 43 belongs to another process: stale
settle(0.3)
wasStopped = stopped()
precondition(!wasStopped && streams.count == base)
screenGranted = false
hold(77, 42, 5)
settle(0.2)
screenGranted = true
wasStopped = stopped()
precondition(!wasStopped && streams.count == base)

// ---- The badge never holds the input back, and late results are discarded ------------------
// The window query is stuck: the hold returns at once, so the input after it is not delayed.
base = streams.count
contentBlocked = true
var began = Date()
hold(77, 42, 5)
precondition(Date().timeIntervalSince(began) < 0.2, "hold waited for the window query")
precondition(streams.count == base)
wasStopped = stopped()
precondition(wasStopped)                         // a start in flight is cancelled
contentBlocked = false
settle(0.3)
precondition(streams.count == base && liveStreams().isEmpty, "a cancelled start created a stream")

// The stream start is stuck: still no wait, and replacing the window discards the late start.
startBlocked = true
began = Date()
hold(77, 42, 5)
precondition(Date().timeIntervalSince(began) < 0.2, "hold waited for the stream start")
waitUntil("pending stream") { streams.count == base + 1 }
hold(77, 42, 5)                                  // same window while starting: only an extension
settle(0.1)
precondition(streams.count == base + 1)
hold(77, 43, 5)                                  // replaced before the first start finished
waitUntil("replacement stream") { streams.count == base + 2 }
startBlocked = false
waitUntil("late start stopped") { streams[base].stopCount == 1 }
precondition(streams[base + 1].isLive && liveStreams().count == 1)
_ = stopped()

// Stopped while the start is in flight: the late start is stopped as soon as it lands.
base = streams.count
startBlocked = true
hold(77, 42, 5)
waitUntil("pending stream") { streams.count == base + 1 }
wasStopped = stopped()
precondition(wasStopped)
startBlocked = false
waitUntil("late start stopped") { streams[base].stopCount == 1 }
precondition(liveStreams().isEmpty)

// The lease runs out before the start lands: same outcome.
base = streams.count
startBlocked = true
hold(77, 42, 0.05)
waitUntil("pending stream") { streams.count == base + 1 }
settle(0.25)
startBlocked = false
waitUntil("late start stopped") { streams[base].stopCount == 1 }
precondition(liveStreams().isEmpty)

// Input integration: a click on a normal window marks it; a popup on top marks nothing.
base = streams.count
holdIndicatorForInput("click", ["x": 10, "y": 10])
waitUntil("input badge") { streams.count == base + 1 && streams[base].isLive }
precondition(streams[base].filter.window.windowID == 42)
holdIndicatorForInput("click", ["x": 120, "y": 120])
holdIndicatorForInput("focus_app", ["pid": 88])
settle(0.2)
precondition(streams.count == base + 1 && streams[base].isLive, "a non-window input changed the badge")
holdIndicator(axWindowFrame: CGRect(x: 600, y: 0, width: 300, height: 300), pid: 88)
waitUntil("ax badge") { streams.count == base + 2 && streams[base + 1].isLive }
precondition(streams[base + 1].filter.window.windowID == 50 && streams[base].stopCount == 1)
holdIndicator(axWindowFrame: nil, pid: 88)
holdIndicator(axWindowFrame: CGRect(x: 7, y: 7, width: 7, height: 7), pid: 88)
settle(0.2)
precondition(streams.count == base + 2 && streams[base + 1].isLive)
_ = stopped()

// indicator_stop takes no fields.
do { _ = try indicatorStop(["seconds": 1]); fatalError("stop accepted a field") } catch OpError.bad { }
let idle = try indicatorStop([:])["stopped"] as? Bool
precondition(idle == false)

// Concurrency: leases expiring on the timer queue race fresh holds and late starts. Every
// stream that started must end exactly once, and none outlives the last lease.
let racing = streams.count
for round in 0..<150 {
    hold(77, round % 2 == 0 ? 42 : 43, 0.003)
    settle(Double(round % 5) * 0.002)
}
_ = stopped()
waitUntil("every stream ended") {
    streams[racing...].allSatisfy { $0.startCount == 1 && $0.stopCount == 1 }
}
precondition(liveStreams().isEmpty, "a stream outlived its lease")
print("capture indicator contract passed")
"""


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None, reason="needs Swift on macOS"
)
def test_capture_indicator_window_decisions_and_lease(tmp_path: Path) -> None:
    source = lifecycle.SOURCE.read_text()
    numbers = (
        "func inputNumber(" + source.split("func inputNumber(", 1)[1].split("func inputBool(", 1)[0]
    )
    capture = (
        "private func captureCallback"
        + source.split("private func captureCallback", 1)[1].split(
            "@available(macOS 14.0, *)\nprivate func capturedWindow", 1
        )[0]
    )
    indicator = (
        "// MARK: - Capture indicator"
        + source.split("// MARK: - Capture indicator", 1)[1].split("// MARK: - Dispatch", 1)[0]
    )
    program = tmp_path / "indicator.swift"
    program.write_text(_HARNESS + numbers + capture + indicator + _CHECKS)
    result = run_bounded(["swift", str(program)], timeout=120, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "capture indicator contract passed"
