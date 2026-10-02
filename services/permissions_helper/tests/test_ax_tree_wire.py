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


def test_ax_tree_request_forwards_a_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    client.ax_tree("Finder", scope="e12", max_nodes=50)
    assert seen[0][1]["scope"] == "e12"
    assert seen[0][1]["max_nodes"] == 50


def test_swift_gates_ax_tree_on_the_accessibility_grant_and_advertises_it() -> None:
    gated = _SWIFT.split("let axGatedMethods: Set<String> = [", 1)[1].split("]", 1)[0]
    assert '"ax_tree"' in gated
    assert 'case "ax_tree": result = try axTree(req)' in _SWIFT
    assert '"ax_tree_v1": true' in _SWIFT


def test_swift_walk_is_bounded_and_never_echoes_secure_fields() -> None:
    walk = _SWIFT.split("func axTree(", 1)[1].split("// MARK: - Root keeper", 1)[0]
    assert "AXUIElementSetMessagingTimeout" in walk
    assert "timeIntervalSince(started) > budget" in walk
    assert "nodes.count >= maxNodes" in walk
    assert "depth + 1 >= maxDepth" in walk
    assert 'node["subrole"] as? String != "AXSecureTextField"' in walk
