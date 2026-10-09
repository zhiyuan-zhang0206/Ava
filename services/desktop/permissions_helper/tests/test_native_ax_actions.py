"""Execute actual axAct logic with inert AX IPC and real opaque-ref equality.

AXUIElementCreateApplication only allocates local opaque refs. Every AX IPC
read/write in the extracted production functions is replaced before execution;
AXValue and CFEqual operations inspect local values, never the user desktop.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

from .. import lifecycle


def section(source: str, start: str, end: str) -> str:
    return start + source.split(start, 1)[1].split(end, 1)[0]


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None, reason="needs Swift on macOS"
)
def test_native_ax_actions_window_identity_selection_and_reported_actions(tmp_path: Path) -> None:
    source = lifecycle._SOURCE.read_text()
    actual = "\n".join(
        [
            section(source, "func numericDouble(", "// Accessibility grant gate."),
            section(source, "private let axTreeAttributes:", "private let axVisibleChildrenRoles:"),
            section(
                source, "private let axMaxStringLength =", "private let axTreeFrameworkMarkers:"
            ),
            section(source, "private struct AXEntry {", "/// Raw ids"),
            section(source, "private func fnv1a(", "private func axFrameworkHint("),
            section(source, "private func axSelectionRange(", "// MARK: - Root keeper"),
        ]
    )
    replacements = {
        "AXUIElementGetPid": "inertGetPid",
        "AXUIElementSetMessagingTimeout": "inertTimeout",
        "AXUIElementCreateApplication": "inertApplication",
        "AXUIElementCopyAttributeValue": "inertAttribute",
        "AXUIElementCopyMultipleAttributeValues": "inertBatch",
        "AXUIElementCopyActionNames": "inertActions",
        "AXUIElementPerformAction": "inertPerform",
        "AXUIElementIsAttributeSettable": "inertSettable",
        "AXUIElementSetAttributeValue": "inertSetAttribute",
    }
    # Fail before Swift execution if production adds an unhandled AX IPC call.
    calls = set(re.findall(r"\b(AXUIElement\w+)\s*\(", actual))
    assert calls <= replacements.keys() | {"AXUIElementGetTypeID"}, calls
    for name, replacement in replacements.items():
        actual = re.sub(rf"\b{name}\b", replacement, actual)
    harness = r"""
import Foundation
import CoreFoundation
import CoreGraphics
import ApplicationServices
enum OpError: Error { case bad(String) }
struct RunningApplication {
    let localizedName: String? = "Editor"
    let bundleIdentifier: String? = "test.editor"
    let processIdentifier: pid_t = 9001
}
class NSWorkspace {
    static let shared = NSWorkspace()
    let runningApplications = [RunningApplication()]
}
func refreshAppKit() { }
// Creating these refs performs no accessibility reads or writes.
let appRef = AXUIElementCreateApplication(9001)
let windowRef = AXUIElementCreateApplication(9002)
let elementRef = AXUIElementCreateApplication(9003)
let otherWindow = AXUIElementCreateApplication(9004)
var windows = [windowRef]
var elementWindow: AXUIElement? = windowRef
var freshValues: [AnyObject] = []
var offered = [String]()
var canSetRange = true
var pidValue: pid_t = 9001
var mutationStatus = AXError.success
var mutations = [String]()
var selectedRange: CFRange?
var batchReads = 0
var elementWindowReads = 0
private var axElementTable: [Int: AXEntry] = [:]
func inertApplication(_ pid: pid_t) -> AXUIElement {
    precondition(pid == 9001)
    return appRef
}
func inertGetPid(_ element: AXUIElement, _ pid: inout pid_t) -> AXError {
    pid = pidValue
    return .success
}
func inertTimeout(_ element: AXUIElement, _ timeout: Float) -> AXError { .success }
func inertAttribute(_ element: AXUIElement, _ attribute: CFString,
                    _ result: inout CFTypeRef?) -> AXError {
    if CFEqual(element, appRef) && attribute as String == kAXWindowsAttribute {
        result = windows as CFArray
        return .success
    }
    if attribute as String == kAXWindowAttribute {
        elementWindowReads += 1
        guard let window = elementWindow else { return .noValue }
        result = window
        return .success
    }
    preconditionFailure("unexpected attribute query")
}
func inertBatch(_ element: AXUIElement, _ attributes: CFArray,
                _ options: AXCopyMultipleAttributeOptions, _ result: inout CFArray?) -> AXError {
    batchReads += 1
    result = freshValues as CFArray
    return .success
}
func inertActions(_ element: AXUIElement, _ result: inout CFArray?) -> AXError {
    result = offered as CFArray
    return .success
}
func inertPerform(_ element: AXUIElement, _ action: CFString) -> AXError {
    mutations.append("action:" + (action as String))
    return mutationStatus
}
func inertSettable(_ element: AXUIElement, _ attribute: CFString,
                   _ settable: inout DarwinBoolean) -> AXError {
    settable = DarwinBoolean(canSetRange)
    return .success
}
func inertSetAttribute(_ element: AXUIElement, _ attribute: CFString,
                       _ value: CFTypeRef) -> AXError {
    mutations.append("attribute:" + (attribute as String))
    if attribute as String == kAXSelectedTextRangeAttribute {
        precondition(CFGetTypeID(value) == AXValueGetTypeID())
        var range = CFRange()
        precondition(AXValueGetValue(value as! AXValue, .cfRange, &range))
        selectedRange = range
    }
    return mutationStatus
}
func reset() {
    windows = [windowRef]; elementWindow = windowRef
    offered = []; canSetRange = true; pidValue = 9001
    mutationStatus = .success; mutations = []; selectedRange = nil
    batchReads = 0; elementWindowReads = 0
    freshValues = ["AXTextField" as NSString, "AXSearchField" as NSString,
                   "Editor" as NSString, NSNull(), "text" as NSString,
                   "field-id" as NSString, NSNull(), NSNull(), NSNull(), NSNull(),
                   NSNull(), NSNull(), NSNull()]
    install()
}
func install(_ element: AXUIElement = elementRef) {
    let values: [AnyObject?] = freshValues.map { $0 }
    axElementTable = [7: AXEntry(element: element, sig: axSignature(role: axString(values[0]),
        values: values), window: windowRef)]
}
func request(_ action: String, _ extra: [String: Any] = [:]) -> [String: Any] {
    var result: [String: Any] = ["app": "Editor", "id": 7, "action": action]
    result.merge(extra) { $1 }
    return result
}
func stale(_ action: String, _ extra: [String: Any] = [:]) throws {
    let result = try axAct(request(action, extra))
    precondition(mutations.isEmpty, "stale target was mutated")
    precondition(result["stale"] as? Bool == true && result["completed"] as? Bool == false)
}
func refused(_ action: String, _ extra: [String: Any] = [:], containing: String) {
    do { _ = try axAct(request(action, extra)); preconditionFailure("expected refusal") }
    catch OpError.bad(let message) {
        precondition(message.contains(containing))
        precondition(!message.contains("private-context"))
    } catch { preconditionFailure("unexpected error") }
    precondition(mutations.isEmpty)
}
"""
    checks = r"""
precondition(CFEqual(windowRef, windowRef) && !CFEqual(windowRef, otherWindow))
reset(); windows = []; offered = ["AXIncrement"]
try stale("perform_action", ["native_action": "AXIncrement"])
precondition(batchReads == 0)
reset(); elementWindow = otherWindow
try stale("select_text", ["text": "text"])
precondition(batchReads == 0)
reset(); elementWindow = nil
try stale("select_text", ["text": "text"])
precondition(batchReads == 0)
reset(); pidValue = 9010; offered = ["AXIncrement"]
try stale("perform_action", ["native_action": "AXIncrement"])
precondition(batchReads == 0)
reset(); freshValues[2] = "Changed editor" as NSString
try stale("select_text", ["text": "text"])
reset(); axElementTable = [:]
try stale("perform_action", ["native_action": "AXIncrement"])
reset(); offered = ["AXCustomApplicationAction"]
let performed = try axAct(request("perform_action", ["native_action": "AXCustomApplicationAction"]))
precondition(performed["completed"] as? Bool == true)
precondition(mutations == ["action:AXCustomApplicationAction"])
reset(); refused("unknown", containing: "unknown ax_act action")
reset(); offered = ["AXDecrement"]
refused("perform_action", ["native_action": "AXIncrement"], containing: "does not offer")
reset(); freshValues[1] = "AXSecureTextField" as NSString
refused("select_text", ["text": "text"], containing: "secure")
reset(); canSetRange = false
refused("select_text", ["text": "text"], containing: "not settable")
reset(); freshValues[4] = NSNumber(value: 42)
refused("select_text", ["text": "42"], containing: "no string")
reset()
freshValues[4] = (String(repeating: "x", count: 500) + "\u{1F600}before \u{1F984} after") as NSString
let selected = try axAct(request("select_text", ["text": "\u{1F984}", "prefix": "before ", "suffix": " after"]))
precondition(selected["completed"] as? Bool == true)
precondition(selectedRange?.location == 509 && selectedRange?.length == 2)
precondition(mutations == ["attribute:AXSelectedTextRange"])
let encoded = String(data: try JSONSerialization.data(withJSONObject: selected), encoding: .utf8)!
precondition(selected["text"] == nil && selected["prefix"] == nil && selected["suffix"] == nil)
precondition(!encoded.contains("before ") && !encoded.contains(" after"))
reset(); freshValues[4] = "text text" as NSString
refused("select_text", ["text": "text", "prefix": "private-context"], containing: "no exact match")
reset(); offered = ["AXCustomApplicationAction"]; mutationStatus = .cannotComplete
let unanswered = try axAct(request("perform_action", ["native_action": "AXCustomApplicationAction"]))
precondition(unanswered["completed"] as? Bool == false && unanswered["unanswered"] as? Bool == true)
precondition(mutations == ["action:AXCustomApplicationAction"])
reset(); freshValues[0] = "AXWindow" as NSString; install(windowRef)
elementWindow = nil; offered = ["AXRaise"]
let raised = try axAct(request("perform_action", ["native_action": "AXRaise"]))
precondition(raised["completed"] as? Bool == true && mutations == ["action:AXRaise"])
precondition(elementWindowReads == 0)
print("native AX action contract passed")
"""
    program = tmp_path / "actions.swift"
    program.write_text(harness + actual + checks)
    result = run_bounded(["swift", str(program)], timeout=60, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native AX action contract passed"
