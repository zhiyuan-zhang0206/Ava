"""The `ax_tree` wire contract between client.py and helper/main.swift.

The live accessibility walk needs a desktop and a granted helper; these tests
pin the request shape the client sends and the guard rails the Swift source
must keep (grant gate, capability bit, bounded walk, secure fields).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services.permissions_helper import client

_SWIFT = (Path(__file__).parents[1] / "helper" / "main.swift").read_text()


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_call(method: str, **args: Any) -> dict[str, Any]:
        seen.append((method, args))
        return {}

    monkeypatch.setattr(client, "_call", fake_call)
    return seen


def test_ax_tree_request_carries_the_bounds_and_omits_an_absent_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture(monkeypatch)
    client.ax_tree("Finder")
    method, args = seen[0]
    assert method == "ax_tree"
    assert args["app"] == "Finder"
    assert (args["max_nodes"], args["max_depth"], args["budget_ms"], args["timeout_ms"]) == (
        600,
        14,
        1500,
        400,
    )
    assert "scope" not in args
    assert args["enable_ax"] is True


def test_ax_tree_request_forwards_a_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    client.ax_tree("Finder", scope=12, scope_fp="abc", max_nodes=50)
    assert (seen[0][1]["scope"], seen[0][1]["scope_fp"]) == (12, "abc")
    assert seen[0][1]["max_nodes"] == 50


def test_ax_act_request_carries_the_value_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    client.ax_act("Mail", 7, "press")
    client.ax_act("Mail", 7, "set_value", value="hi")
    (method, plain), (_, with_value) = seen
    assert method == "ax_act"
    assert (plain["app"], plain["id"], plain["action"]) == ("Mail", 7, "press")
    assert "value" not in plain
    assert with_value["value"] == "hi"


def test_swift_gates_ax_tree_on_the_accessibility_grant_and_advertises_it() -> None:
    gated = _SWIFT.split("let axGatedMethods: Set<String> = [", 1)[1].split("]", 1)[0]
    assert '"ax_tree"' in gated
    assert 'case "ax_tree": result = try axTree(req)' in _SWIFT
    assert '"ax_tree_v1": true' in _SWIFT


def test_swift_walk_is_bounded_and_never_echoes_secure_fields() -> None:
    walk = _SWIFT.split("func axTree(", 1)[1].split("func axAct(", 1)[0]
    assert "AXUIElementSetMessagingTimeout" in walk
    assert "timeIntervalSince(started) > budget" in walk
    assert "nodes.count >= maxNodes" in walk
    assert "depth + 1 >= maxDepth" in walk
    assert 'node["subrole"] as? String != "AXSecureTextField"' in walk


def test_swift_ax_act_gates_validates_and_never_echoes_the_value() -> None:
    gated = _SWIFT.split("let axGatedMethods: Set<String> = [", 1)[1].split("]", 1)[0]
    assert '"ax_act"' in gated
    assert 'case "ax_act": result = try axAct(req)' in _SWIFT
    assert '"ax_act_v1": true' in _SWIFT
    act = _SWIFT.split("func axAct(", 1)[1].split("// MARK: - Root keeper", 1)[0]
    # A recycled or vanished element answers stale instead of being acted on.
    assert "axSignature(role: role, values: values) == entry.sig" in act
    assert "AXUIElementIsAttributeSettable" in act
    assert "AXUIElementSetMessagingTimeout" in act
    assert 'result["value"]' not in act and "\\(text)" not in act


def test_swift_fingerprints_exclude_values_and_chain_from_the_parent() -> None:
    walk = _SWIFT.split("func axTree(", 1)[1].split("func axAct(", 1)[0]
    assert 'ordinalKey = parentFp + "/" + segment' in walk
    assert "siblingCounts[ordinalKey" in walk
    discriminator = _SWIFT.split("private func axDiscriminator", 1)[1].split("private func", 1)[0]
    assert "values[4]" not in discriminator  # index 4 is the value attribute


def test_swift_enables_chromium_accessibility_narrowly() -> None:
    enable = _SWIFT.split("private func axEnableChromiumAccessibility", 1)[1].split(
        "private func axPickWindow", 1
    )[0]
    assert '"AXManualAccessibility" as CFString' in enable
    assert "axEnabledPids.contains(pid)" in enable  # sticky: no second set, no second wait
    assert '"AXEnhancedUserInterface"' not in _SWIFT  # VoiceOver's switch; changes native windows
    walk = _SWIFT.split("func axTree(", 1)[1].split("func axAct(", 1)[0]
    assert '!framework.isEmpty && req["scope"] == nil' in walk  # only unscoped, only Chromium
    assert 'req["enable_ax"] as? Bool ?? true' in walk and '"off"' in walk
    for name in ('"chromium"', '"electron"', '"cef"'):
        markers = _SWIFT.split("private let axTreeFrameworkMarkers", 1)[1].split("\n]", 1)[0]
        assert name in markers


def test_swift_detects_renamed_chromium_forks_by_their_renderer_helper() -> None:
    hint = _SWIFT.split("private func axFrameworkHint", 1)[1].split("private func axPickWindow", 1)[
        0
    ]
    assert 'hasSuffix("Helper (Renderer).app")' in hint
    assert 'appendingPathComponent(entry + "/Helpers")' in hint
