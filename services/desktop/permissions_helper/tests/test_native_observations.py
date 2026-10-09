"""Execute native observation parsing/freshness with inert AppKit/SCK substitutes."""

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
def test_native_inventory_and_window_capture_identity(tmp_path: Path) -> None:
    source = lifecycle._SOURCE.read_text()
    inventory = (
        "private func refreshAppKit("
        + source.split("private func refreshAppKit(", 1)[1].split(
            "/// Explicit foreground activation", 1
        )[0]
    )
    capture = (
        "private func captureCallback"
        + source.split("private func captureCallback", 1)[1].split("/// Coerce a JSON number", 1)[0]
    )
    numbers = (
        "func inputNumber(" + source.split("func inputNumber(", 1)[1].split("func inputBool(", 1)[0]
    )
    harness = r"""
import Foundation
import CoreGraphics
import CoreFoundation
enum OpError: Error { case bad(String) }
var screenGranted = true
func CGPreflightScreenCaptureAccess() -> Bool { screenGranted }
class NSRunningApplication {
    let processIdentifier: pid_t
    let localizedName: String?
    let bundleIdentifier: String?
    let isTerminated = false
    init(_ pid: pid_t, _ name: String?, _ bundle: String?) {
        processIdentifier = pid; localizedName = name; bundleIdentifier = bundle
    }
}
class NSWorkspace {
    static let shared = NSWorkspace()
    var runningApplications = [NSRunningApplication(77, nil, "org.test.target")]
}
var windowRows: [[String: Any]] = [
    [kCGWindowLayer as String: 0, kCGWindowOwnerPID as String: 77, kCGWindowNumber as String: 42,
     kCGWindowBounds as String: ["X": 30, "Y": 40, "Width": 80, "Height": 40],
     kCGWindowIsOnscreen as String: false],
    [kCGWindowLayer as String: 1, kCGWindowOwnerPID as String: 77, kCGWindowNumber as String: 43,
     kCGWindowBounds as String: ["X": 0, "Y": 0, "Width": 100, "Height": 100]]
]
func CGWindowListCopyWindowInfo(_ option: CGWindowListOption, _ id: CGWindowID) -> CFArray? {
    windowRows as CFArray
}
class SCApplication { let processID: pid_t; init(_ pid: pid_t) { processID = pid } }
class SCWindow {
    let windowID: UInt32
    let owningApplication: SCApplication?
    let frame: CGRect
    init(_ id: UInt32, _ pid: pid_t, _ frame: CGRect) {
        windowID = id; owningApplication = SCApplication(pid); self.frame = frame
    }
}
var contents: [[SCWindow]] = []
var captureCalls = 0
class SCShareableContent {
    let windows: [SCWindow]
    init(_ rows: [SCWindow]) { windows = rows }
    static func getExcludingDesktopWindows(_ exclude: Bool, onScreenWindowsOnly: Bool,
                                          completionHandler: @escaping (SCShareableContent?, Error?) -> Void) {
        precondition(exclude && !onScreenWindowsOnly)
        captureCalls += 1
        let rows = contents.removeFirst()
        // Actually deliver on another queue: the production callback wait uses a lock and bounded run loop.
        DispatchQueue.global().async { completionHandler(SCShareableContent(rows), nil) }
    }
}
class SCContentFilter {
    let contentRect: CGRect
    let pointPixelScale: Float = 2
    init(desktopIndependentWindow: SCWindow) { contentRect = desktopIndependentWindow.frame }
}
class SCStreamConfiguration {
    var width = 0; var height = 0
    var showsCursor = true
    var ignoreShadowsSingleWindow = false
}
class CGImage {
    let width: Int; let height: Int
    init(_ width: Int, _ height: Int) { self.width = width; self.height = height }
}
class SCScreenshotManager {
    static func captureImage(contentFilter: SCContentFilter, configuration: SCStreamConfiguration,
                             completionHandler: @escaping (CGImage?, Error?) -> Void) {
        precondition(!configuration.showsCursor && configuration.ignoreShadowsSingleWindow)
        let image = CGImage(configuration.width, configuration.height)
        DispatchQueue.global().async { completionHandler(image, nil) }
    }
}
class NSBitmapImageRep {
    enum FileType { case png }
    init(cgImage: CGImage) { }
    func representation(using: FileType, properties: [String: Any]) -> Data? { Data("inert PNG".utf8) }
}
"""
    checks = r"""
let apps = listApps()["apps"] as! [[String: Any]]
precondition(apps[0]["name"] is NSNull && apps[0]["bundle_id"] as? String == "org.test.target")
let windows = try listWindows(["app": "org.test.target"])["windows"] as! [[String: Any]]
precondition(windows.count == 1 && windows[0]["w"] as? CGFloat == 80)
precondition(windows[0]["title"] is NSNull && windows[0]["on_screen"] as? Bool == false)
NSWorkspace.shared.runningApplications.append(NSRunningApplication(78, "Second", "org.test.target"))
do { _ = try listWindows(["app": "org.test.target"]); fatalError("ambiguous selector accepted") } catch OpError.bad { }
NSWorkspace.shared.runningApplications.removeLast()
screenGranted = false
do { _ = try listWindows([:]); fatalError("metadata permission ignored") } catch OpError.bad { }
screenGranted = true
let rect = CGRect(x: 30, y: 40, width: 100, height: 50)
let window = SCWindow(42, 77, rect)
let path = CommandLine.arguments[1]
let request: [String: Any] = ["pid": 77, "window_id": 42, "path": path]
contents = [[window], [window]]
let result = try capturedWindow(request)
precondition(result["width"] as? Int == 200 && result["height"] as? Int == 100)
precondition((result["origin"] as? [String: CGFloat])?["x"] == 30)
precondition(FileManager.default.fileExists(atPath: path))
try FileManager.default.removeItem(atPath: path)
for fresh in [SCWindow(42, 78, rect), SCWindow(42, 77, CGRect(x: 31, y: 40, width: 100, height: 50))] {
    contents = [[window], [fresh]]
    do { _ = try capturedWindow(request); fatalError("changed identity/geometry accepted") } catch OpError.bad { }
    precondition(!FileManager.default.fileExists(atPath: path))
}
contents = [[SCWindow(42, 78, rect)]]
do { _ = try capturedWindow(request); fatalError("wrong owner accepted") } catch OpError.bad { }
screenGranted = false; captureCalls = 0
do { _ = try capturedWindow(request); fatalError("capture permission ignored") } catch OpError.bad { }
precondition(captureCalls == 0)
print("native observations contract passed")
"""
    program = tmp_path / "observations.swift"
    program.write_text(harness + numbers + inventory + capture + checks)
    result = run_bounded(
        ["swift", str(program), str(tmp_path / "inert.png")],
        timeout=60,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native observations contract passed"
