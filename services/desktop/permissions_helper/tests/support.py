from __future__ import annotations

from pathlib import Path


def stopped_helper_plist(home: Path, path: Path) -> bytes:
    import plistlib

    from services.desktop.permissions_helper import launchd_job as jobs

    body = plistlib.dumps(
        {
            "Label": jobs.helper_job_label(home),
            "ProgramArguments": ["/private/test/AvaPermissionsHelper"],
            "KeepAlive": {"SuccessfulExit": False},
            "EnvironmentVariables": {
                "AVA_PERMISSIONS_HELPER_SOCKET": str(home / "run/permissions-helper.23456.sock"),
                "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(home / "run/ava-root/seed.json"),
            },
        }
    )
    path.write_bytes(body)
    return body


INPUT_HARNESS = r"""
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
var reads = 0
var cursorFailAt = 0
var cursorReadFails = false
var cursorScript: [CGPoint] = []
var cursorDefault = CGPoint(x: -8, y: 9)
var mainOrigin = CGPoint.zero
func CGMainDisplayID() -> CGDirectDisplayID { 1 }
func CGDisplayBounds(_ id: CGDirectDisplayID) -> CGRect {
    CGRect(origin: mainOrigin, size: CGSize(width: 1000, height: 800))
}
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
    /// A cursor read: scripted, never counted as an allocation.
    init?(cursorRead: Void) {
        reads += 1
        if cursorReadFails || reads == cursorFailAt { return nil }
        location = cursorScript.isEmpty ? cursorDefault : cursorScript.removeFirst()
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
    convenience init?(source: Any?) { self.init(cursorRead: ()) }
    func setIntegerValueField(_ field: CGEventField, value: Int64) { clickState = value }
    func post(tap: CGEventTapLocation) { posted.append(self) }
}
func reset() {
    posted = []; created = 0; failAt = 0; Thread.waits = []
    reads = 0; cursorFailAt = 0; cursorReadFails = false; cursorScript = []
    cursorDefault = CGPoint(x: -8, y: 9); stubCalls = 0
}

var stubCalls = 0
var monitorStarts = 0
var monitorStops = 0
func startHeldMouseMonitor() { monitorStarts += 1 }
func stopHeldMouseMonitor() { monitorStops += 1 }
var abortStops = 0
@discardableResult func stopCaptureIndicator() -> Bool { abortStops += 1; return true }
"""


def helper_source() -> str:
    from services.desktop.permissions_helper import lifecycle

    return lifecycle.SOURCE.read_text()


def between(source: str, start: str, end: str) -> str:
    return start + source.split(start, 1)[1].split(end, 1)[0]


def input_bodies(source: str) -> str:
    return between(source, "func rejectUnknownFields(", "func logIndicator(") + between(
        source, "func inputNumber(", "/// Post a synthetic mouse click"
    )


def dispatch_stubs(stubbed: list[str]) -> str:
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
/// Each badge request: the method, and how many events had been posted by then.
var holdCalls: [(method: String, postedBefore: Int)] = []
func holdIndicatorForInput(_ method: String, _ req: [String: Any]) {
    holdCalls.append((method, posted.count))
}
func requestHelperShutdown(runDir: String) throws -> [String: Any] { [:] }
"""
    for name in stubbed:
        stubs += (
            f"func {name}(_ req: [String: Any]) throws -> [String: Any] "
            "{ stubCalls += 1; return [:] }\n"
        )
    for name in ["listApps", "sessionInfo", "frontmostApp"]:
        stubs += f"func {name}() -> [String: Any] {{ stubCalls += 1; return [:] }}\n"
    return stubs
