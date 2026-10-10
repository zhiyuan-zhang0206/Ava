"""Tests for the `ax_tree` tool (services/desktop/computer/ax_tools.py).

Filtering, truncation, rendering and the quality verdict are pure functions of
the helper's raw result, tested here on hand-built node lists. The tool entry
runs against a faked helper through the daemon dispatch. The live accessibility
walk itself (the Swift side) needs a desktop and is not exercised here.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate

from ...permissions_helper import client as helper
from ...permissions_helper.client import AxNode, AxTreeResult
from .. import ax_tools as ax
from ..errors import ComputerUseError
from ..mcp_daemon import ComputerMcpDaemon
from .slices import computer_use_config


def node(
    nid: int,
    parent: int | None,
    role: str,
    *,
    depth: int,
    n: int = 0,
    frame: tuple[float, float, float, float] | None = (10.0, 20.0, 100.0, 40.0),
    **attrs: Any,
) -> AxNode:
    out: dict[str, Any] = {
        "id": nid,
        "fp": f"fp{nid}",
        "depth": depth,
        "n": n,
        "role": role,
        **attrs,
    }
    if parent is not None:
        out["parent"] = parent
    if frame is not None:
        out["x"], out["y"], out["w"], out["h"] = frame
    return out  # pyright: ignore[reportReturnType]


WINDOW = (0.0, 0.0, 1000.0, 800.0)


def window(n: int) -> AxNode:
    return node(1, None, "AXWindow", depth=0, n=n, frame=WINDOW, title="Compose")


def result(nodes: list[AxNode], **overrides: Any) -> AxTreeResult:
    base: dict[str, Any] = {
        "app": "Mail",
        "pid": 1,
        "windows": 1,
        "framework": "",
        "ax_enable": "n/a",
        "nodes": nodes,
        "visited": len(nodes),
        "truncated": False,
        "timed_out": False,
        "unreadable": 0,
        "elapsed_ms": 5,
    }
    return {**base, **overrides}  # pyright: ignore[reportReturnType]


def compose_window() -> list[AxNode]:
    """A window with a wrapper group, a duplicate label, a disabled control, and
    an offscreen and a zero-size element."""
    return [
        window(5),
        node(2, 1, "AXGroup", depth=1, n=3),  # unlabeled wrapper: collapsed
        node(3, 1, "AXButton", depth=1, n=1, title="Send", frame=(20.0, 30.0, 60.0, 20.0)),
        node(4, 1, "AXTextField", depth=1, title="To", value="a@b.co", focused=True),
        node(5, 1, "AXButton", depth=1, title="Draft", enabled=False),
        node(6, 1, "AXButton", depth=1, title="Hidden", frame=(5000.0, 5000.0, 10.0, 10.0)),
        node(7, 2, "AXButton", depth=2, title="Cancel"),
        node(8, 2, "AXButton", depth=2, title="Empty", frame=(0.0, 0.0, 0.0, 0.0)),
        node(9, 3, "AXStaticText", depth=2, value="Send", frame=(25.0, 32.0, 30.0, 10.0)),
        node(10, 2, "AXStaticText", depth=2, value="Subject line"),
    ]


def shown(nodes: list[AxNode], mode: ax.Mode = "interactive", cap: int = 150) -> str:
    kept, hidden = ax.select(nodes, mode, cap)
    return ax.render(nodes, kept, hidden, 2.0, mode)


def test_interactive_render_collapses_filters_and_converts_to_physical_pixels() -> None:
    assert shown(compose_window()) == "\n".join(
        [
            '[e1] window "Compose" @1000,800 2000x1600',
            '  [e7] button "Cancel" @120,80 200x80',
            '  [e10] statictext "Subject line" @120,80 200x80',
            '  [e3] button "Send" @100,80 120x40',
            '  [e4] textfield "To" @120,80 200x80 {focused} val="a@b.co"',
            '  [e5] button "Draft" @120,80 200x80 {disabled}',
        ]
    )


def test_full_mode_keeps_everything_and_shows_actions() -> None:
    nodes = compose_window()
    nodes[2]["actions"] = ["AXPress"]
    nodes[2]["ident"] = "send-btn"
    text = shown(nodes, "full")
    assert '[e8] button "Empty"' in text
    assert '[e6] button "Hidden"' in text
    assert '[e9] statictext "Send"' in text
    assert '[e3] button "Send" @100,80 120x40 #send-btn AXPress' in text


def test_text_mode_keeps_only_readable_text() -> None:
    text = shown(compose_window(), "text")
    assert '[e10] statictext "Subject line"' in text
    assert '[e4] textfield "To"' in text
    assert "button" not in text


def test_cap_keeps_controls_before_labels_and_marks_the_cut() -> None:
    text = shown(compose_window(), cap=2)
    lines = text.splitlines()
    assert any("[e3] button" in line for line in lines)
    assert any("[e4] textfield" in line for line in lines)
    assert "statictext" not in text
    assert lines[-1] == '  ... +3 more under e1 (scope="e1")'


def test_children_the_helper_never_read_are_counted_under_their_kept_ancestor() -> None:
    nodes = [window(1), node(2, 1, "AXButton", depth=1, n=5, title="More")]
    assert shown(nodes).splitlines()[-1] == '    ... +5 more under e2 (scope="e2")'


def test_a_label_that_only_repeats_its_parent_is_dropped() -> None:
    nodes = [
        window(1),
        node(2, 1, "AXButton", depth=1, n=1, title="OK"),
        node(3, 2, "AXStaticText", depth=2, value="OK"),
    ]
    assert "statictext" not in shown(nodes)


def test_label_quoting() -> None:
    nodes = [window(1), node(2, 1, "AXButton", depth=1, title='Say "hi"\nnow')]
    assert '[e2] button "Say \\"hi\\" now"' in shown(nodes)


def test_subrole_is_part_of_the_role_name() -> None:
    nodes = [window(1), node(2, 1, "AXTextField", depth=1, subrole="AXSearchField", title="Find")]
    assert '[e2] textfield:searchfield "Find"' in shown(nodes)


# ── quality ─────────────────────────────────────────────────────────────────


def test_quality_ok_for_a_usable_tree() -> None:
    q = ax.assess_quality(result(compose_window()), scoped=False)
    assert q["ok"] is True and q["reason"] is None and q["suggest"] is None
    assert q["interactive"] >= 3


def test_quality_no_window() -> None:
    q = ax.assess_quality(result([]), scoped=False)
    assert q["ok"] is False and q["reason"] == "no_window"
    assert "snapshot" in q["suggest"]


def test_quality_sparse_vs_electron_not_exposed() -> None:
    nodes = [window(1), node(2, 1, "AXGroup", depth=1)]
    assert ax.assess_quality(result(nodes), scoped=False)["reason"] == "sparse"
    flagged = ax.assess_quality(result(nodes, framework="electron"), scoped=False)
    assert flagged["reason"] == "electron_not_exposed"


@pytest.mark.parametrize(
    ("ax_enable", "reason"),
    [
        ("off", "electron_ax_disabled"),
        ("failed", "electron_enable_failed"),
        ("set", "electron_not_exposed"),
        ("already", "electron_not_exposed"),
    ],
)
def test_chromium_apps_get_a_reason_that_says_what_the_enable_step_did(
    ax_enable: str, reason: str
) -> None:
    nodes = [window(1), node(2, 1, "AXGroup", depth=1)]
    q = ax.assess_quality(result(nodes, framework="chromium", ax_enable=ax_enable), scoped=False)
    assert q["ok"] is False and q["reason"] == reason
    assert reason in q["suggest"] and "include_ocr_gap" in q["suggest"]


def test_a_helper_that_predates_ax_enable_still_gets_a_quality_verdict() -> None:
    walk = result([window(1), node(2, 1, "AXGroup", depth=1)], framework="electron")
    del walk["ax_enable"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
    assert ax.assess_quality(walk, scoped=False)["reason"] == "electron_not_exposed"


def test_an_unreadable_or_timed_out_empty_walk_is_unresponsive_not_no_window() -> None:
    assert ax.assess_quality(result([], unreadable=1), scoped=False)["reason"] == "unresponsive"
    assert ax.assess_quality(result([], timed_out=True), scoped=False)["reason"] == "unresponsive"
    assert ax.assess_quality(result([]), scoped=False)["reason"] == "no_window"


def test_quality_canvas() -> None:
    nodes = [window(1), node(2, 1, "AXImage", depth=1, frame=(0.0, 0.0, 900.0, 700.0))]
    q = ax.assess_quality(result(nodes), scoped=False)
    assert q["ok"] is False and q["reason"] == "canvas"


def test_quality_partial_and_timeout_stay_usable() -> None:
    q = ax.assess_quality(result(compose_window(), timed_out=True), scoped=False)
    assert q["ok"] is True and q["partial"] is True and q["reason"] == "timeout"
    q = ax.assess_quality(result(compose_window(), truncated=True), scoped=False)
    assert q["ok"] is True and q["partial"] is True and q["reason"] is None


def test_scoped_walk_is_not_judged_sparse() -> None:
    nodes = [node(7, None, "AXGroup", depth=0)]
    assert ax.assess_quality(result(nodes), scoped=True)["ok"] is True


# ── tool entry through the daemon ───────────────────────────────────────────


class FakeAxHelper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.supported = True
        self.tree = result(compose_window())

    def ping(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("ping", {}))
        return {"pong": True, "ax_tree_v1": True} if self.supported else {"pong": True}

    def frontmost_app(self, **kw: Any) -> dict[str, Any]:
        return {"app": "Mail"}

    def screen_size(self, **kw: Any) -> dict[str, Any]:
        return {"x": 0.0, "y": 0.0, "w": 1512.0, "h": 982.0, "scale": 2.0}

    def ax_tree(self, app: str, **kw: Any) -> AxTreeResult:
        self.calls.append(("ax_tree", {"app": app, **kw}))
        return self.tree


@pytest.fixture
def fake_ax(monkeypatch: pytest.MonkeyPatch) -> FakeAxHelper:
    fake = FakeAxHelper()
    for name in ("ping", "frontmost_app", "screen_size", "ax_tree"):
        monkeypatch.setattr(helper, name, getattr(fake, name))
    return fake


async def call_ax(
    args: dict[str, Any] | None = None,
    daemon: ComputerMcpDaemon | None = None,
    *,
    database_gate: ProcessDbGate,
) -> dict[str, Any]:
    daemon = daemon or ComputerMcpDaemon(
        computer_use_config(),
        Database.from_settings(gate=database_gate),
        sock="/nonexistent-test.sock",
    )
    resp = await daemon._dispatch(
        {"id": 1, "method": "call_tool", "tool": "ax_tree", "args": args or {}, "agent_id": None}
    )
    if resp["ok"] is False:
        raise ComputerUseError(resp["error"])
    return json.loads(resp["result"]["content"][0]["text"])


async def test_ax_tree_defaults_to_the_frontmost_app_and_renders(
    fake_ax: FakeAxHelper, *, database_gate: ProcessDbGate
) -> None:
    out = await call_ax(database_gate=database_gate)
    assert out["app"] == "Mail"
    assert out["quality"]["ok"] is True
    assert out["tree"].startswith('[e1] window "Compose" @1000,800 2000x1600')
    assert fake_ax.calls[-1][1]["app"] == "Mail"
    assert fake_ax.calls[-1][1]["scope"] is None


async def test_ax_tree_scope_goes_through_the_raw_id_and_widens_the_walk(
    fake_ax: FakeAxHelper, database: Database, *, database_gate: ProcessDbGate
) -> None:
    daemon = ComputerMcpDaemon(computer_use_config(), database, sock="/nonexistent-test.sock")
    await call_ax({"app": "Mail"}, daemon, database_gate=database_gate)
    await call_ax(
        {"app": "Mail", "scope": "e2", "max_nodes": 300}, daemon, database_gate=database_gate
    )
    call = fake_ax.calls[-1][1]
    assert (call["app"], call["scope"], call["scope_fp"], call["max_nodes"]) == (
        "Mail",
        2,
        "fp2",
        1200,
    )


async def test_ax_tree_scope_without_a_prior_walk_is_refused(
    fake_ax: FakeAxHelper, *, database_gate: ProcessDbGate
) -> None:
    with pytest.raises(ComputerUseError, match="call ax_tree first"):
        await call_ax({"app": "Mail", "scope": "e2"}, database_gate=database_gate)
    assert all(name != "ax_tree" for name, _ in fake_ax.calls)


async def test_ax_tree_unusable_window_returns_the_verdict_and_no_tree(
    fake_ax: FakeAxHelper, *, database_gate: ProcessDbGate
) -> None:
    fake_ax.tree = result([])
    out = await call_ax(database_gate=database_gate)
    assert out["tree"] == "" and out["quality"]["reason"] == "no_window"


async def test_ax_tree_on_an_old_helper_fails_with_the_rebuild_instruction(
    fake_ax: FakeAxHelper, *, database_gate: ProcessDbGate
) -> None:
    fake_ax.supported = False
    with pytest.raises(ComputerUseError, match="predates ax_tree"):
        await call_ax(database_gate=database_gate)
    assert all(name != "ax_tree" for name, _ in fake_ax.calls)


@pytest.mark.parametrize(
    "args",
    [{"mode": "everything"}, {"max_nodes": 0}, {"max_nodes": 401}, {"scope": "12"}],
)
async def test_ax_tree_rejects_bad_arguments(
    fake_ax: FakeAxHelper, args: dict[str, Any], *, database_gate: ProcessDbGate
) -> None:
    with pytest.raises(ComputerUseError):
        await call_ax(args, database_gate=database_gate)
    assert fake_ax.calls == []


async def test_ax_tree_is_declared_in_the_tool_list(database: Database) -> None:
    daemon = ComputerMcpDaemon(computer_use_config(), database, sock="/nonexistent-test.sock")
    resp = await daemon._dispatch({"id": 1, "method": "list_tools"})
    assert resp["ok"] is True
    assert resp["ok"] is True
    tools: list[dict[str, Any]] = resp["result"]
    assert "ax_tree" in {t["name"] for t in tools}


@pytest.mark.parametrize(
    "action", ["AXIncrement", "AXDecrement", "AXCancel", "AXExpand", "AXApplicationAction"]
)
def test_interactive_tree_exposes_platform_actions_without_a_fixed_action_allowlist(
    action: str,
) -> None:
    nodes = [window(1), node(2, 1, "AXUnknown", depth=1, actions=[action])]
    assert f"[e2] unknown @120,80 200x80 {action}" in shown(nodes)


def test_context_menu_only_nodes_do_not_become_interactive_controls() -> None:
    nodes = [window(1), node(2, 1, "AXCell", depth=1, actions=["AXShowMenu"])]
    assert "[e2]" not in shown(nodes)
    assert "AXShowMenu" in shown(nodes, "full")
