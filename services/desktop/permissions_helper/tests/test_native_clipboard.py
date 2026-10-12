"""Execute the native clipboard bodies against an inert pasteboard, never the user's clipboard."""

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
enum OpError: Error { case bad(String) }
struct PasteboardType: Hashable {
    let rawValue: String
    static let string = PasteboardType(rawValue: "public.utf8-plain-text")
    static let png = PasteboardType(rawValue: "public.png")
}
/// Stands in for NSPasteboard: a keyed store, with a switch that makes the system refuse a write.
final class NSPasteboard {
    static let general = NSPasteboard()
    var items: [PasteboardType: String] = [:]
    var order: [PasteboardType] = []
    var acceptWrites = true
    var types: [PasteboardType]? { order.isEmpty ? nil : order }
    func string(forType type: PasteboardType) -> String? { items[type] }
    @discardableResult func clearContents() -> Int { items = [:]; order = []; return 1 }
    func setString(_ value: String, forType type: PasteboardType) -> Bool {
        guard acceptWrites else { return false }
        items[type] = value; order.append(type); return true
    }
    func seed(_ type: PasteboardType, _ value: String) { items[type] = value; order.append(type) }
}
"""


def _sources() -> str:
    source = lifecycle._SOURCE.read_text()
    unknown = (
        "func rejectUnknownFields("
        + source.split("func rejectUnknownFields(", 1)[1].split("func logIndicator(", 1)[0]
    )
    clipboard = (
        "// MARK: - Clipboard"
        + source.split("// MARK: - Clipboard", 1)[1].split("/// Report the on-screen geometry", 1)[
            0
        ]
    )
    return _HARNESS + unknown + clipboard


def _run(tmp_path: Path, checks: str) -> str:
    path = tmp_path / "clipboard.swift"
    path.write_text(_sources() + checks)
    result = run_bounded(["swift", str(path)], timeout=60, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[:400]
    return result.stdout.strip()


@_NEEDS_SWIFT
def test_native_clipboard_read_reports_text_types_and_absence(tmp_path: Path) -> None:
    checks = r"""
let board = NSPasteboard.general

// An empty clipboard has no text: null, never an invented empty string.
var read = try clipboardRead([:])
precondition(read["text"] is NSNull && read["length"] as? Int == 0)
precondition(read["truncated"] as? Bool == false && (read["types"] as? [String]) == [])

// Only a non-text type: still no text, and the type is listed.
board.seed(.png, "binary")
read = try clipboardRead([:])
precondition(read["text"] is NSNull && (read["types"] as? [String]) == ["public.png"])

// Text plus another type: the text is returned and every type is listed.
board.seed(.string, "héllo \u{4E16}\u{754C} \u{1F600}")
read = try clipboardRead([:])
precondition(read["text"] as? String == "héllo \u{4E16}\u{754C} \u{1F600}")
precondition(read["length"] as? Int == 10, "length counts code points, as Python len does")
precondition((read["types"] as? [String]) == ["public.png", "public.utf8-plain-text"])
precondition(read["truncated"] as? Bool == false)

// Present but empty text is "", distinct from no text.
board.clearContents(); board.seed(.string, "")
read = try clipboardRead([:])
precondition(read["text"] as? String == "" && read["length"] as? Int == 0)

// Text beyond the bound is cut, flagged, and still reports its whole length.
board.clearContents(); board.seed(.string, String(repeating: "a", count: clipboardMaxCharacters + 5))
read = try clipboardRead([:])
precondition(read["truncated"] as? Bool == true)
precondition((read["text"] as? String)?.unicodeScalars.count == clipboardMaxCharacters)
precondition(read["length"] as? Int == clipboardMaxCharacters + 5)

// Exactly at the bound is not truncated.
board.clearContents(); board.seed(.string, String(repeating: "a", count: clipboardMaxCharacters))
read = try clipboardRead([:])
precondition(read["truncated"] as? Bool == false)

// Reading changes nothing.
precondition(board.order == [.string])

// Arguments are refused, with the field named and nothing read.
do { _ = try clipboardRead(["text": "x"]); fatalError("argument accepted") }
catch OpError.bad(let message) { precondition(message == "unknown field: text") }
print("clipboard read passed")
"""
    assert _run(tmp_path, checks) == "clipboard read passed"


@_NEEDS_SWIFT
def test_native_clipboard_write_replaces_everything_and_never_echoes_the_text(
    tmp_path: Path,
) -> None:
    checks = r"""
let board = NSPasteboard.general
let sentinel = "do-not-leak-this-text"

// A write replaces every type with the one plain-text item and reports a length only.
board.seed(.png, "binary"); board.seed(.string, "old")
var result = try clipboardWrite(["text": "héllo \u{4E16}\u{754C} \u{1F600}"])
precondition(board.order == [.string] && board.items[.string] == "héllo \u{4E16}\u{754C} \u{1F600}")
precondition(result["length"] as? Int == 10 && result.count == 1, "the text must not be echoed")

// An empty text is a valid write.
result = try clipboardWrite(["text": ""])
precondition(board.items[.string] == "" && result["length"] as? Int == 0)

// Exactly at the bound is accepted.
_ = try clipboardWrite(["text": String(repeating: "a", count: clipboardMaxCharacters)])

// Invalid input is refused before the clipboard is touched, and no error carries the text.
board.clearContents(); board.seed(.string, "kept")
func refused(_ request: [String: Any], _ expected: String) {
    do { _ = try clipboardWrite(request); fatalError("accepted \(request.keys)") }
    catch OpError.bad(let message) {
        precondition(message == expected, message)
        precondition(!message.contains(sentinel), "error carried the text")
    } catch { fatalError("\(error)") }
    precondition(board.items[.string] == "kept" && board.order == [.string], "board touched")
}
refused(["text": sentinel + String(repeating: "a", count: clipboardMaxCharacters)],
        "clipboard text exceeds \(clipboardMaxCharacters) characters")
refused(["text": 123], "clipboard_write needs string text")
refused(["text": NSNull()], "clipboard_write needs string text")
refused([:], "clipboard_write needs string text")
refused(["text": sentinel, "password": sentinel], "unknown field: password")

// The system refusing the write is an error, not a silent success, and still no text in it.
board.acceptWrites = false
do { _ = try clipboardWrite(["text": sentinel]); fatalError("refused write reported as done") }
catch OpError.bad(let message) { precondition(message == "the clipboard did not accept the text") }
print("clipboard write passed")
"""
    assert _run(tmp_path, checks) == "clipboard write passed"
