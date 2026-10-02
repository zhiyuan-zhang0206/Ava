"""Tests for stable element ids (ax_ids.py) and the `ax_act` tool (ax_act.py).

`FakeAxApp` simulates the helper's side of the contract: every walk numbers the
nodes afresh (raw ids), the raw ids of the latest walk are the only ones that
can be acted on, and an element that left the window or changed answers
`stale`. The real accessibility calls are not exercised here.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from services.computer.ax_ids import AxIdTable, AxSession
from services.computer.errors import ComputerUseError
from services.computer.mcp_daemon import ComputerMcpDaemon
from services.permissions_helper import client as helper
from services.permissions_helper.client import AxNode, PermissionsHelperError

TYPED_TEXT = "hunter2-s3cret"


def el(fp: str, role: str, parent: str | None, title: str = "", **kw: Any) -> dict[str, Any]:
    return {
        "fp": fp,
        "role": role,
        "parent": parent,
        "title": title,
        "frame": (10, 20, 100, 40),
        **kw,
    }


class FakeAxApp:
    """The helper's element table and walk numbering for one app."""

    def __init__(self) -> None:
        self.elements: list[dict[str, Any]] = [
            el("win", "AXWindow", None, "Compose"),
            el("send", "AXButton", "win", "Send", actions=["AXPress"]),
            el("to", "AXTextField", "win", "To", settable=True),
            el("note", "AXStaticText", "win", "Footer"),
        ]
        self.pid = 4242
        self.next_raw = 100
        self.table: dict[int, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.acted: list[tuple[str, str, str | None]] = []
        self.caps = {"ax_tree_v1": True, "ax_act_v1": True}
        self.unanswered = False
        self.act_error: str | None = None

    # -- scripting
    def remove(self, fp: str) -> None:
        self.elements = [e for e in self.elements if e["fp"] != fp]

    def add(self, element: dict[str, Any]) -> None:
        self.elements.append(element)

    def forget_table(self) -> None:
        """Another walk (e.g. of another app) replaced the helper's table."""
        self.table = {}

    # -- helper surface
    def ping(self, **kw: Any) -> dict[str, Any]:
        return {"pong": True, **self.caps}

    def frontmost_app(self, **kw: Any) -> dict[str, Any]:
        return {"app": "Mail"}

    def screen_size(self, **kw: Any) -> dict[str, Any]:
        return {"x": 0.0, "y": 0.0, "w": 1512.0, "h": 982.0, "scale": 2.0}

    def ax_tree(self, app: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("ax_tree", {"app": app, **kw}))
        scope_raw = kw.get("scope")
        if scope_raw is None:
            self.table = {}
            members = list(self.elements)
        else:
            top = self.table[scope_raw]
            members = [e for e in self.elements if e["fp"] == top or self._under(e, top)]
        raw_of: dict[str, int] = {}
        nodes: list[AxNode] = []
        for depth_first in members:
            raw = self.next_raw
            self.next_raw += 1
            raw_of[depth_first["fp"]] = raw
            self.table[raw] = depth_first["fp"]
            node: dict[str, Any] = {
                "id": raw,
                "fp": depth_first["fp"],
                "depth": 0 if depth_first["parent"] is None else 1,
                "n": sum(1 for e in self.elements if e["parent"] == depth_first["fp"]),
                "role": depth_first["role"],
                "title": depth_first["title"],
                "actions": depth_first.get("actions", []),
            }
            node["x"], node["y"], node["w"], node["h"] = (float(v) for v in depth_first["frame"])
            if depth_first["parent"] in raw_of:
                node["parent"] = raw_of[depth_first["parent"]]
            nodes.append(node)  # pyright: ignore[reportArgumentType]
        return {  # pyright: ignore[reportReturnType]
            "app": app,
            "pid": self.pid,
            "windows": 1,
            "framework": "",
            "ax_enable": "n/a",
            "nodes": nodes,
            "visited": len(nodes),
            "truncated": False,
            "timed_out": False,
            "unreadable": 0,
            "elapsed_ms": 1,
        }

    def _under(self, element: dict[str, Any], top: str) -> bool:
        return element["parent"] == top

    def ax_act(
        self, app: str, raw_id: int, action: str, *, value: str | None = None, **kw: Any
    ) -> dict[str, Any]:
        self.calls.append(("ax_act", {"app": app, "raw_id": raw_id, "action": action}))
        fp = self.table.get(raw_id)
        found = next((e for e in self.elements if e["fp"] == fp), None)
        if found is None:
            return {"completed": False, "stale": True}
        if self.act_error is not None:
            raise PermissionsHelperError(self.act_error)
        self.acted.append((found["fp"], action, value))
        out: dict[str, Any] = {
            "action": action,
            "completed": not self.unanswered,
            "role": found["role"],
            "label": found["title"],
        }
        out["x"], out["y"], out["w"], out["h"] = (float(v) for v in found["frame"])
        if self.unanswered:
            out["unanswered"] = True
        return out


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FakeAxApp:
    fake = FakeAxApp()
    for name in ("ping", "frontmost_app", "screen_size", "ax_tree", "ax_act"):
        monkeypatch.setattr(helper, name, getattr(fake, name))
    return fake


@pytest.fixture
def audit_log(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []

    def record(event: Any) -> None:
        log.append({"event_type": event.event_name, "payload": event.attributes})

    monkeypatch.setattr("base.agents.impersonation_manifest.emit_recorded_central_event", record)
    return log


@pytest.fixture
def daemon() -> ComputerMcpDaemon:
    return ComputerMcpDaemon(sock="/nonexistent-test.sock")


async def call(
    daemon: ComputerMcpDaemon, tool: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    resp = await daemon._dispatch(
        {"id": 1, "method": "call_tool", "tool": tool, "args": args or {}, "agent_id": 7}
    )
    if resp["ok"] is False:
        raise ComputerUseError(resp["error"])
    return json.loads(resp["result"]["content"][0]["text"])


def ids_in(tree: str) -> dict[str, str]:
    """label -> element id for the lines of a rendered tree."""
    out: dict[str, str] = {}
    for line in tree.splitlines():
        head, _, rest = line.strip().partition(" ")
        if '"' in rest:
            out[rest.split('"')[1]] = head.strip("[]")
    return out


# ── ax_ids: alignment ───────────────────────────────────────────────────────


def raw_node(raw: int, fp: str, parent: int | None = None) -> AxNode:
    node: dict[str, Any] = {"id": raw, "fp": fp, "depth": 0, "n": 0}
    if parent is not None:
        node["parent"] = parent
    return node  # pyright: ignore[reportReturnType]


def test_same_fingerprint_keeps_its_id_across_walks_and_new_ones_get_new_ids() -> None:
    table = AxIdTable(app="Mail", pid=1)
    first = table.align([raw_node(10, "a"), raw_node(11, "b", 10)], scoped=False)
    assert [n["id"] for n in first] == [1, 2] and first[1].get("parent") == 1
    second = table.align(
        [raw_node(50, "c"), raw_node(51, "b", 50), raw_node(52, "a", 51)], scoped=False
    )
    assert [n["id"] for n in second] == [3, 2, 1]
    assert second[1].get("parent") == 3 and second[2].get("parent") == 2
    assert table.raw_of(1) == 52 and table.raw_of(2) == 51


def test_an_element_that_left_the_walk_loses_its_raw_id_but_keeps_its_stable_id() -> None:
    table = AxIdTable(app="Mail", pid=1)
    table.align([raw_node(1, "a"), raw_node(2, "b", 1)], scoped=False)
    table.align([raw_node(9, "a")], scoped=False)
    with pytest.raises(ComputerUseError, match="not in the latest ax_tree"):
        table.raw_of(2)
    table.align([raw_node(20, "a"), raw_node(21, "b", 20)], scoped=False)
    assert table.raw_of(2) == 21  # scrolled back: same id


def test_a_scoped_walk_refreshes_only_its_subtree() -> None:
    table = AxIdTable(app="Mail", pid=1)
    table.align([raw_node(1, "a"), raw_node(2, "b", 1), raw_node(3, "c", 1)], scoped=False)
    table.align([raw_node(30, "b"), raw_node(31, "d", 30)], scoped=True)
    assert table.raw_of(2) == 30 and table.raw_of(1) == 1 and table.raw_of(3) == 3
    assert table.entry(4).fp == "d"


def test_a_fingerprint_collision_inside_one_walk_never_shares_an_id() -> None:
    table = AxIdTable(app="Mail", pid=1)
    out = table.align([raw_node(1, "dup"), raw_node(2, "dup", 1)], scoped=False)
    assert out[0]["id"] != out[1]["id"]


def test_unknown_ids_fail_with_the_instruction() -> None:
    with pytest.raises(ComputerUseError, match="call ax_tree first"):
        AxIdTable(app="Mail", pid=1).entry(5)


def test_a_new_pid_or_app_starts_a_fresh_table() -> None:
    session = AxSession()
    first = session.table_for("Mail", 1)
    first.align([raw_node(1, "a")], scoped=False)
    assert session.table_for("Mail", 1) is first
    assert session.table_for("Mail", 2) is not first
    with pytest.raises(ComputerUseError, match="call ax_tree first"):
        session.current("Notes")


# ── ax_act ──────────────────────────────────────────────────────────────────


async def test_press_acts_on_the_element_and_reports_its_center_in_physical_pixels(
    app: FakeAxApp, daemon: ComputerMcpDaemon
) -> None:
    ids = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    out = await call(daemon, "ax_act", {"id": ids["Send"], "action": "press"})
    assert app.acted == [("send", "press", None)]
    assert out == {
        "element": ids["Send"],
        "action": "press",
        "completed": True,
        "role": "AXButton",
        "label": "Send",
        "x": 120,
        "y": 80,
    }


async def test_ids_stay_the_same_when_the_app_renumbers_and_a_new_element_appears(
    app: FakeAxApp, daemon: ComputerMcpDaemon
) -> None:
    first = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    app.add(el("cc", "AXTextField", "win", "Cc", settable=True))
    second = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    assert {k: second[k] for k in first} == first
    assert second["Cc"] not in first.values()


async def test_a_stale_raw_id_is_refound_by_fingerprint_and_retried_once(
    app: FakeAxApp, daemon: ComputerMcpDaemon
) -> None:
    ids = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    app.forget_table()  # the helper's table was replaced since
    app.calls.clear()
    await call(daemon, "ax_act", {"id": ids["Send"], "action": "press"})
    assert [name for name, _ in app.calls] == ["ax_act", "ax_tree", "ax_act"]
    assert app.acted == [("send", "press", None)]


async def test_an_element_that_is_gone_is_never_guessed_at(
    app: FakeAxApp, daemon: ComputerMcpDaemon
) -> None:
    ids = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    app.remove("send")
    with pytest.raises(ComputerUseError, match="gone or changed"):
        await call(daemon, "ax_act", {"id": ids["Send"], "action": "press"})
    assert app.acted == []


async def test_unknown_id_and_missing_walk_fail_before_any_helper_call(
    app: FakeAxApp, daemon: ComputerMcpDaemon
) -> None:
    with pytest.raises(ComputerUseError, match="call ax_tree first"):
        await call(daemon, "ax_act", {"id": "e1", "action": "press"})
    await call(daemon, "ax_tree", {"app": "Mail"})
    app.calls.clear()
    with pytest.raises(ComputerUseError, match="unknown element e99"):
        await call(daemon, "ax_act", {"id": "e99", "action": "press"})
    assert app.calls == []


async def test_a_restarted_app_invalidates_every_id(
    app: FakeAxApp, daemon: ComputerMcpDaemon
) -> None:
    ids = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    app.forget_table()
    app.pid = 9999
    with pytest.raises(ComputerUseError, match="was restarted"):
        await call(daemon, "ax_act", {"id": ids["Send"], "action": "press"})


async def test_helper_errors_surface_unchanged(app: FakeAxApp, daemon: ComputerMcpDaemon) -> None:
    ids = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    app.act_error = "element does not offer AXPress (it offers: )"
    with pytest.raises(ComputerUseError, match="does not offer AXPress"):
        await call(daemon, "ax_act", {"id": ids["Footer"], "action": "press"})


async def test_an_unanswered_action_is_reported_as_possibly_done(
    app: FakeAxApp, daemon: ComputerMcpDaemon
) -> None:
    ids = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    app.unanswered = True
    out = await call(daemon, "ax_act", {"id": ids["Send"], "action": "press"})
    assert out["completed"] is False and "may still have run" in out["note"]


async def test_old_helper_gets_the_rebuild_instruction(
    app: FakeAxApp, daemon: ComputerMcpDaemon
) -> None:
    ids = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    app.caps["ax_act_v1"] = False
    with pytest.raises(ComputerUseError, match="predates ax_act"):
        await call(daemon, "ax_act", {"id": ids["Send"], "action": "press"})


@pytest.mark.parametrize(
    "args",
    [
        {"id": "12", "action": "press"},
        {"id": "e1", "action": "click"},
        {"id": "e1", "action": "set_value"},
        {"id": "e1", "action": "set_value", "value": 5},
        {"id": "e1", "action": "set_value", "value": "x" * 10_001},
        {"id": "e1", "action": "press", "value": "x"},
    ],
)
async def test_bad_arguments_are_rejected_before_the_helper(
    app: FakeAxApp, daemon: ComputerMcpDaemon, args: dict[str, Any]
) -> None:
    with pytest.raises(ComputerUseError):
        await call(daemon, "ax_act", args)
    assert app.calls == []


# ── set_value and the audit trail ───────────────────────────────────────────


async def test_set_value_reaches_the_field_and_is_not_echoed_or_audited(
    app: FakeAxApp, daemon: ComputerMcpDaemon, audit_log: list[dict[str, Any]]
) -> None:
    ids = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    out = await call(
        daemon, "ax_act", {"id": ids["To"], "action": "set_value", "value": TYPED_TEXT}
    )
    assert app.acted == [("to", "set_value", TYPED_TEXT)]
    assert TYPED_TEXT not in json.dumps(out)
    acted = [e for e in audit_log if e["payload"]["action"] == "ax_act"]
    assert [e["payload"]["outcome"] for e in acted] == ["ok"]
    assert acted[0]["payload"]["coords"] == "120,80,set_value"
    assert TYPED_TEXT not in json.dumps(audit_log)


async def test_a_failed_set_value_leaves_the_value_out_of_the_audit_too(
    app: FakeAxApp, daemon: ComputerMcpDaemon, audit_log: list[dict[str, Any]]
) -> None:
    ids = ids_in((await call(daemon, "ax_tree", {"app": "Mail"}))["tree"])
    app.act_error = "element value is not settable"
    with pytest.raises(ComputerUseError):
        await call(
            daemon, "ax_act", {"id": ids["Footer"], "action": "set_value", "value": TYPED_TEXT}
        )
    failed = [e for e in audit_log if e["payload"]["action"] == "ax_act"]
    assert [e["payload"]["outcome"] for e in failed] == ["error"]
    assert TYPED_TEXT not in json.dumps(audit_log)


async def test_ax_act_is_declared_with_its_required_arguments() -> None:
    daemon = ComputerMcpDaemon(sock="/nonexistent-test.sock")
    resp = await daemon._dispatch({"id": 1, "method": "list_tools"})
    assert resp["ok"] is True
    tools: list[dict[str, Any]] = resp["result"]
    declared = next(t for t in tools if t["name"] == "ax_act")
    assert declared["input_schema"]["required"] == ["id", "action"]
