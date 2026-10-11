"""Computer mcp daemon cases: click text measures scale and tracks pointer."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate

from ....permissions_helper.client import PermissionsHelperError
from ... import mcp_daemon as daemon_mod
from ... import screen as screen_mod
from ...protocol import Response
from ..slices import computer_use_config, short_sock_dir
from ..test_computer_mcp_daemon import (
    SHORT_SESSION,
    FakeHelper,
    FakeOcr,
    _call,
    _daemon,
    _ok_result,
)
from ..test_computer_mcp_daemon import (
    audit_log as audit_log,
)
from ..test_computer_mcp_daemon import (
    fake_helper as fake_helper,
)
from ..test_computer_mcp_daemon import (
    fake_ocr as fake_ocr,
)


async def test_click_text_measures_scale_and_tracks_pointer(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """The stale-helper regression, applied to click_text: a capture on a 1x
    display (helper claims 2x) must pass click coordinates through UNCHANGED,
    and a later click must convert with the scale click_text measured."""
    fh = fake_helper
    fh.screen = {"x": 0.0, "y": 0.0, "w": 1920.0, "h": 1080.0, "scale": 2.0}  # stale claim
    fh.png_size = (1920, 1080)  # the truth: 1x
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/click-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon(database_gate=database_gate)
    result = await _ok_result(d, "click_text", {"text": "search"})
    assert result["scale"] == 1.0
    assert ("click", {"x": 540.0, "y": 112.0, "double": False}) in fh.calls
    # A user may move the cursor after click_text: read its live logical position.
    await _ok_result(d, "scroll", {"dy": -10})
    assert ("scroll", {"x": 321.0, "y": 123.0, "dy": -10}) in fh.calls
    # a plain click converts with the 1x scale click_text measured, not the
    # helper's stale 2x claim
    await _call(d, "click", {"x": 81, "y": 15})
    assert ("click", {"x": 81.0, "y": 15.0, "double": False}) in fh.calls


async def test_type_key_scroll_window_session(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    d = _daemon(database_gate=database_gate)
    assert (await _ok_result(d, "type_text", {"text": "\u4f60\u597d"}))["typed"] == 2
    assert (await _ok_result(d, "key", {"key": "return", "cmd": True}))["pressed"] == 36
    assert (await _ok_result(d, "scroll", {"x": 5, "y": 6, "dy": -20}))["scrolled"] == -20
    assert (await _ok_result(d, "window_info", {"owner": "Finder"}))["owner"] == "Finder"
    assert (await _ok_result(d, "session_info"))["locked"] is False


async def test_helper_failure_surfaces_as_error(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    def _boom(*args: Any, **kw: Any) -> Any:
        raise PermissionsHelperError("helper down")

    monkeypatch.setattr(daemon_mod.helper, "click", _boom)
    d = _daemon(database_gate=database_gate)
    resp = await _call(d, "click", {"x": 1, "y": 2})
    assert resp["ok"] is False
    assert "helper down" in resp["error"]


async def test_success_result_is_call_tool_result_dump(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    """The daemon's call_tool result validates as an MCP CallToolResult —
    the exact contract the per-agent wrapper and the direct dial enforce, and
    the shape acceptance caught missing (regression for #2139)."""
    from mcp import types

    d = _daemon(database_gate=database_gate)
    resp = await _call(d, "frontmost_app")
    assert resp["ok"] is True
    result = types.CallToolResult.model_validate(resp["result"])
    assert result.is_error is False
    assert [b.type for b in result.content] == ["text"]
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert json.loads(block.text) == {"app": "Finder"}


async def test_missing_required_argument_fails_cleanly(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    """A missing required argument is a readable tool error, not a bare
    KeyError leaking from the helper call."""
    d = _daemon(database_gate=database_gate)
    resp = await _call(d, "click", {"y": 2})
    assert resp["ok"] is False
    assert "click requires argument 'x'" in resp["error"]
    assert audit_log[0]["payload"]["outcome"] == "error"
    resp2 = await _call(d, "scroll", {"x": 1, "y": 2})
    assert resp2["ok"] is False
    assert "scroll requires dx or dy" in resp2["error"]
    assert audit_log[1]["payload"]["outcome"] == "error"


async def test_window_info_defaults_owner_to_frontmost(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    """window_info without owner uses the frontmost app."""
    d = _daemon(database_gate=database_gate)
    result = await _ok_result(d, "window_info")
    assert result["owner"] == "Finder"
    assert ("window_info", {"owner": "Finder"}) in fake_helper.calls


async def test_key_accepts_names_characters_and_keycodes(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    """The key tool takes a key name, a single character, or a raw keycode."""
    d = _daemon(database_gate=database_gate)
    assert (await _ok_result(d, "key", {"key": "a"}))["pressed"] == 0
    assert (await _ok_result(d, "key", {"key": "RETURN"}))["pressed"] == 36
    assert (await _ok_result(d, "key", {"key": "F5"}))["pressed"] == 96
    assert (await _ok_result(d, "key", {"key": "up"}))["pressed"] == 126
    assert (await _ok_result(d, "key", {"keycode": 36}))["pressed"] == 36
    calls = [c for c in fake_helper.calls if c[0] == "key"]
    assert calls == [
        ("key", {"code": 0, "cmd": False}),
        ("key", {"code": 36, "cmd": False}),
        ("key", {"code": 96, "cmd": False}),
        ("key", {"code": 126, "cmd": False}),
        ("key", {"code": 36, "cmd": False}),
    ]


async def test_key_unknown_name_fails_cleanly(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    d = _daemon(database_gate=database_gate)
    resp = await _call(d, "key", {"key": "wibble"})
    assert resp["ok"] is False
    assert "unknown key name" in resp["error"]
    assert audit_log[0]["payload"]["outcome"] == "error"
    resp2 = await _call(d, "key")
    assert resp2["ok"] is False
    assert "key requires exactly one" in resp2["error"]


async def test_key_result_maps_helper_echo_to_pressed(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    """The daemon's key response carries "pressed" even though the helper
    echoes {"key": code, "cmd": ...} — the contract callers read."""
    d = _daemon(database_gate=database_gate)
    result = await _ok_result(d, "key", {"key": "return", "cmd": True})
    assert result == {"pressed": 36, "cmd": True}


async def test_scroll_uses_live_cursor_after_click(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    """Scroll follows a cursor moved since the last synthetic click."""
    d = _daemon(database_gate=database_gate)
    await _ok_result(d, "click", {"x": 100, "y": 200})
    result = await _ok_result(d, "scroll", {"dy": -10})
    assert result == {"scrolled": -10}
    assert ("scroll", {"x": 321.0, "y": 123.0, "dy": -10}) in fake_helper.calls


async def test_scroll_uses_live_cursor_before_first_click(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    d = _daemon(database_gate=database_gate)
    result = await _ok_result(d, "scroll", {"dy": 5})
    assert result == {"scrolled": 5}
    # Live cursor is already logical; never divide it by the screenshot scale.
    assert ("scroll", {"x": 321.0, "y": 123.0, "dy": 5}) in fake_helper.calls


async def test_scroll_live_cursor_overrides_previous_explicit_position(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    d = _daemon(database_gate=database_gate)
    await _ok_result(d, "scroll", {"x": 40, "y": 60, "dy": -5})
    result = await _ok_result(d, "scroll", {"dy": -1})
    assert result == {"scrolled": -1}
    # The second call ignores the previous explicit point when the cursor has moved.
    assert ("scroll", {"x": 321.0, "y": 123.0, "dy": -1}) in fake_helper.calls


async def test_audit_emitted_on_success(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    monkeypatch.setattr(screen_mod, "_snapshot_path", lambda _agent_id: "/tmp/x.png")  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    d = _daemon(database_gate=database_gate)
    await _call(d, "click", {"x": 100, "y": 200, "task_id": 42})
    assert len(audit_log) == 2  # pyright: ignore[reportUnknownArgumentType]
    start, ev = audit_log
    assert start["event_type"] == "computer_session_start"
    assert start["payload"]["task_id"] == 42
    assert ev["event_type"] == "computer_action"
    assert ev["agent_id"] == 7
    assert ev["source"] == "agent:7"
    assert ev["payload"]["action"] == "click"
    assert ev["payload"]["outcome"] == "ok"
    assert ev["payload"]["coords"] == "100,200"
    assert ev["payload"]["task_id"] == 42
    assert ev["payload"]["app"] == "Finder"


async def test_audit_emitted_on_error(
    fake_helper: FakeHelper, audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    d = _daemon(database_gate=database_gate)
    await _call(d, "key", {"key": "wibble"})
    assert len(audit_log) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert audit_log[0]["payload"]["outcome"] == "error"
    assert "unknown key name" in audit_log[0]["payload"]["error"]


async def test_no_audit_row_for_anonymous_call(
    audit_log: list, *, database_gate: ProcessDbGate
) -> None:
    d = _daemon(database_gate=database_gate)
    await _call(d, "click", {"x": 1, "y": 2}, agent_id=None)
    assert audit_log == []


async def test_concurrent_calls_are_safe(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """Concurrent dispatches from different connections both complete and are
    both audited. Execution is synchronous and wrapped in the machine-wide
    action lock — the lock is the guard for future async points inside a call
    (e.g. Phase 2's queue), and the sync body already prevents interleaving."""
    monkeypatch.setattr(screen_mod, "_snapshot_path", lambda _a: "/tmp/x.png")  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    d = _daemon(database_gate=database_gate)
    t1 = asyncio.create_task(_call(d, "click", {"x": 1, "y": 2}))
    t2 = asyncio.create_task(_call(d, "snapshot"))
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert len(audit_log) == 2  # pyright: ignore[reportUnknownArgumentType]
    # both actions reached the helper, in some order
    kinds = {c[0] for c in fake_helper.calls}
    assert {"click", "screencapture_region"} <= kinds


async def test_screen_busy_blocks_second_agent(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    d = _daemon(**SHORT_SESSION, database_gate=database_gate)
    # agent 7 takes the screen with a click
    assert (await _call(d, "click", {"x": 1, "y": 2}, agent_id=7))["ok"] is True
    # agent 8's action waits past the tiny queue timeout and fails busy
    resp = await _call(d, "click", {"x": 3, "y": 4}, agent_id=8)
    assert resp["ok"] is False
    assert "screen busy" in resp["error"]
    assert fake_helper.calls.count(("click", {"x": 1.5, "y": 2.0, "double": False})) == 0


async def test_holder_continues_while_busy(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    d = _daemon(**SHORT_SESSION, database_gate=database_gate)
    await _call(d, "click", {"x": 1, "y": 2}, agent_id=7)
    # the holder's own next action passes through (lease renewed by the call)
    resp = await _call(d, "type_text", {"text": "hi"}, agent_id=7)
    assert resp["ok"] is True


async def test_release_control_hands_over(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    d = _daemon(**SHORT_SESSION, database_gate=database_gate)
    await _call(d, "click", {"x": 1, "y": 2}, agent_id=7)

    async def waiter() -> Response:
        return await _call(d, "click", {"x": 3, "y": 4}, agent_id=8)

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.02)  # agent 8 queues up
    rel = await _call(d, "release_control", {}, agent_id=7)
    assert rel["ok"] is True
    assert await t  # agent 8's queued action then runs
    assert any(c[0] == "click" and c[1]["x"] == 1.5 for c in fake_helper.calls)


async def test_release_control_by_non_holder_fails(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    d = _daemon(**SHORT_SESSION, database_gate=database_gate)
    await _call(d, "click", {"x": 1, "y": 2}, agent_id=7)
    resp = await _call(d, "release_control", {}, agent_id=8)
    assert resp["ok"] is False
    assert "not the screen holder" in resp["error"]


async def test_operator_force_release_without_identity(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    d = _daemon(**SHORT_SESSION, database_gate=database_gate)
    await _call(d, "click", {"x": 1, "y": 2}, agent_id=7)
    # CLI path: no agent_id, force=true — releases whoever holds the screen
    resp = await _call(d, "release_control", {"force": True}, agent_id=None)
    assert resp["ok"] is True
    # screen is free again: a fresh agent acts immediately
    resp2 = await _call(d, "click", {"x": 5, "y": 6}, agent_id=9)
    assert resp2["ok"] is True


async def test_task_session_emit_failure_warns_but_action_succeeds(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """A failing session-envelope emit (contract mismatch, FK hiccup) must not
    fail the action nor stay silent — it warns (task #1136)."""
    warnings: list[str] = []
    monkeypatch.setattr(daemon_mod.logger, "warning", lambda msg: warnings.append(str(msg)))  # pyright: ignore[reportUnknownArgumentType]
    log: list[dict[str, Any]] = []

    def _stage(_db: object, event: Any) -> None:
        if event.event_name.startswith("computer_session_"):
            # the envelope path is broken (unregistered name etc.)
            raise ValueError(f"unknown event name {event.event_name!r}")
        log.append(
            {
                "event_type": event.event_name,
                "agent_id": event.agent_id,
                "source": event.source,
                "payload": event.attributes,
            }
        )  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr("base.agents.impersonation.manifest.emit_recorded_central_event", _stage)
    d = _daemon(database_gate=database_gate)
    resp = await _call(d, "click", {"x": 1, "y": 2, "task_id": 42})
    assert resp["ok"] is True  # the action itself executed
    assert any("task-session event failed" in w for w in warnings)
    # the computer_action row still landed (the envelope is auxiliary)
    assert [e["event_type"] for e in log] == ["computer_action"]


async def test_run_raises_stream_limit_for_large_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    d, sock, cleanup = short_sock_dir()
    server_options: dict[str, Any] = {}

    async def socket_not_in_use(_path: Path) -> bool:
        return False

    async def capture_server_options(*_args: Any, **kwargs: Any) -> None:
        server_options.update(kwargs)
        raise RuntimeError("server options captured")

    monkeypatch.setattr(daemon_mod, "_socket_in_use", socket_not_in_use)
    monkeypatch.setattr(daemon_mod.asyncio, "start_unix_server", capture_server_options)

    try:
        # The failure leaves run()'s TaskGroup, so it arrives in an exception group.
        with pytest.RaisesGroup(pytest.RaisesExc(RuntimeError, match="server options captured")):
            await daemon_mod.run(sock=str(sock), database=lambda: database)
        assert server_options["limit"] == 64 * 1024 * 1024
    finally:
        cleanup(d)


async def test_socket_in_use_false_when_nobody_listens() -> None:
    """A stale (or absent) socket is not "in use" — the daemon may unlink it."""
    d, sock, cleanup = short_sock_dir()
    try:
        assert await daemon_mod._socket_in_use(sock) is False
    finally:
        cleanup(d)


async def test_socket_in_use_true_when_listener_present() -> None:
    """A socket with a live listener is "in use" — a second daemon must refuse
    to start instead of unlink-stealing it (the #1137 dual-daemon orphan)."""
    d, sock, cleanup = short_sock_dir()
    server = await asyncio.start_unix_server(lambda _r, w: w.close(), path=str(sock))
    try:
        assert await daemon_mod._socket_in_use(sock) is True
    finally:
        server.close()
        await server.wait_closed()
        cleanup(d)


async def test_shutdown_cancels_active_clients(database: Database) -> None:
    """run()'s shutdown path cancels tracked client handlers, so a client that
    holds its connection open cannot hang server.wait_closed() and orphan the
    daemon process (the #1137 dual-daemon root cause)."""
    d, sock, cleanup = short_sock_dir()
    try:
        daemon = daemon_mod.ComputerMcpDaemon(computer_use_config(), database, sock=str(sock))
        # A client handler that never returns unless cancelled — the persistent
        # SDK connection equivalent (a real client sits in handle()'s readline).
        started = asyncio.Event()

        async def stuck_client(*_a: object) -> None:
            started.set()
            with suppress(Exception):
                await asyncio.Event().wait()  # never completes on its own

        daemon.handle = stuck_client  # type: ignore[method-assign]
        async with asyncio.TaskGroup() as clients:
            # _tracked_client registers the done_callback that removes the task from
            # daemon._clients — the same wiring run() uses.
            daemon_mod._tracked_client(daemon, clients, None, None)  # type: ignore[arg-type]
            await started.wait()

            # run()'s shutdown path: cancel every tracked client, then gather.
            for t in list(daemon._clients):
                t.cancel()
            # gather(return_exceptions=True) swallows the handler's CancelledError —
            # awaiting the task again would re-raise it (suppress(Exception) can't).
            await asyncio.gather(*daemon._clients, return_exceptions=True)
            assert daemon._clients == set()
    finally:
        cleanup(d)


async def test_high_priority_waiter_jumps_the_queue(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """A high-priority call queues ahead of an earlier normal one (Phase 3)."""
    d = _daemon(
        computer_use_lease_s=1.0, computer_use_queue_timeout_s=0.5, database_gate=database_gate
    )
    await _call(d, "click", {"x": 1, "y": 2}, agent_id=7)
    order: list[str] = []

    async def normal() -> None:
        await _call(d, "click", {"x": 3, "y": 4}, agent_id=8)
        order.append("normal")

    async def high() -> None:
        await _call(d, "click", {"x": 5, "y": 6, "priority": "high"}, agent_id=9)
        order.append("high")

    tn = asyncio.create_task(normal())
    await asyncio.sleep(0.02)  # normal queues first
    th = asyncio.create_task(high())
    await asyncio.sleep(0.05)  # both waiters are in the queue now
    await _call(d, "release_control", {}, agent_id=7)
    await asyncio.gather(tn, th)
    assert order == ["high", "normal"]


async def test_snapshot_audit_carries_png_path(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """A snapshot's computer_action row carries the PNG path — the trace
    replay needs it (Phase 3, task #1101)."""
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/snap-trace.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon(database_gate=database_gate)
    await _call(d, "snapshot", {"task_id": 42})
    actions = [ev for ev in audit_log if ev["event_type"] == "computer_action"]
    assert actions[0]["payload"]["action"] == "snapshot"
    assert actions[0]["payload"]["path"] == "/tmp/snap-trace.png"  # noqa: S108
