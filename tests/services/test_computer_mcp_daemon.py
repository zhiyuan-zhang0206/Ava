"""Unit tests for the computer-use MCP daemon (services/computer/mcp_daemon.py).

Covers tool dispatch against a faked permissions helper, the machine-wide
serialization of actions, and the computer_action audit stream. The socket
wiring itself is left to dev-cluster testing (same stance as the browser
daemon tests). No governance-gate tests: per-agent permission division is a
prompt-level peer convention, not code-enforced (user ruling 2026-08-10).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

import services.computer.mcp_daemon as daemon_mod
import services.computer.ocr_text as ocr_text_mod
import services.computer.screen as screen_mod
from services.computer.errors import ComputerUseError
from services.computer.mcp_daemon import ComputerMcpDaemon
from services.computer.protocol import Request, Response
from services.permissions_helper.client import PermissionsHelperError


def _daemon() -> ComputerMcpDaemon:
    return ComputerMcpDaemon(sock="/nonexistent-test.sock")


def _req(
    method: str,
    *,
    tool: str | None = None,
    args: dict[str, Any] | None = None,
    agent_id: int | None = 7,
) -> Request:
    return {
        "id": 1,
        "method": method,
        "tool": tool,
        "args": args or {},
        "agent_id": agent_id,
    }


# ── helper fakes ────────────────────────────────────────────────────────────


class FakeHelper:
    """Records calls; frontmost app + screen geometry are scriptable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.frontmost = "Finder"
        self.screen = {"x": 0.0, "y": 0.0, "w": 1512.0, "h": 982.0, "scale": 2.0}
        # PNG the fake screencapture produces (IHDR) — Retina 2x by default;
        # a stale-helper regression sets it to the 1x 1920x1080 shape.
        self.png_size: tuple[int, int] = (3024, 1964)

    def screen_size(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("screen_size", {}))
        return self.screen

    def frontmost_app(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("frontmost_app", {}))
        return {"app": self.frontmost}

    def screencapture_region(self, x: int, y: int, w: int, h: int, path: str, **kw: Any) -> Any:
        self.calls.append(("screencapture_region", {"x": x, "y": y, "w": w, "h": h, "path": path}))
        pw, ph = self.png_size
        with Path(path).open("wb") as f:
            f.write(
                b"\x89PNG\r\n\x1a\n"
                + b"\x00\x00\x00\x0dIHDR"
                + pw.to_bytes(4, "big")
                + ph.to_bytes(4, "big")
                + b"\x08\x06\x00\x00\x00"
            )
        return {"path": path, "bytes": 42}

    def click(self, x: float, y: float, **kw: Any) -> dict[str, Any]:
        self.calls.append(("click", {"x": x, "y": y, **kw}))
        return {"clicked": {"x": x, "y": y}, "double": bool(kw.get("double"))}

    def type_text(self, text: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("type_text", {"text": text}))
        return {"typed": len(text)}

    def key(self, code: int, **kw: Any) -> dict[str, Any]:
        # Mirrors main.swift's real echo shape {"key": code, "cmd": ...} — the
        # daemon maps it to the MCP "pressed" contract.
        self.calls.append(("key", {"code": code, **kw}))
        return {"key": code, "cmd": bool(kw.get("cmd"))}

    def scroll(self, x: float, y: float, dy: int, **kw: Any) -> dict[str, Any]:
        self.calls.append(("scroll", {"x": x, "y": y, "dy": dy}))
        return {"scrolled": dy}

    def ax_window_info(self, app: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("ax_window_info", {"app": app}))
        return {"app": app, "x": 0.0, "y": 0.0, "w": 800.0, "h": 600.0}

    def window_info(self, owner: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("window_info", {"owner": owner}))
        return {"owner": owner, "x": 0.0, "y": 0.0, "w": 800.0, "h": 600.0}

    def session_info(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("session_info", {}))
        return {"locked": False, "on_console": True}


@pytest.fixture
def fake_helper(monkeypatch: pytest.MonkeyPatch) -> FakeHelper:
    fh = FakeHelper()
    monkeypatch.setattr(daemon_mod.helper, "screen_size", fh.screen_size)
    monkeypatch.setattr(daemon_mod.helper, "frontmost_app", fh.frontmost_app)
    monkeypatch.setattr(daemon_mod.helper, "screencapture_region", fh.screencapture_region)
    monkeypatch.setattr(daemon_mod.helper, "click", fh.click)
    monkeypatch.setattr(daemon_mod.helper, "type_text", fh.type_text)
    monkeypatch.setattr(daemon_mod.helper, "key", fh.key)
    monkeypatch.setattr(daemon_mod.helper, "scroll", fh.scroll)
    monkeypatch.setattr(daemon_mod.helper, "ax_window_info", fh.ax_window_info)
    monkeypatch.setattr(daemon_mod.helper, "window_info", fh.window_info)
    monkeypatch.setattr(daemon_mod.helper, "session_info", fh.session_info)
    return fh


@pytest.fixture
def audit_log(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []

    def _insert(
        *,
        event_type: str,
        agent_id: int | None,
        source: str,
        payload: dict[str, Any],
        **_: Any,
    ) -> None:
        log.append(
            {"event_type": event_type, "agent_id": agent_id, "source": source, "payload": payload}
        )

    monkeypatch.setattr(daemon_mod.audit_events, "insert_event_log", _insert)
    return log


async def _call(
    daemon: ComputerMcpDaemon,
    tool: str,
    args: dict[str, Any] | None = None,
    agent_id: int | None = 7,
) -> Response:
    return await daemon._dispatch(_req("call_tool", tool=tool, args=args, agent_id=agent_id))


async def _ok_result(
    daemon: ComputerMcpDaemon,
    tool: str,
    args: dict[str, Any] | None = None,
    agent_id: int | None = 7,
) -> dict[str, Any]:
    """A successful call's semantic payload (the JSON text block of the
    CallToolResult dump, parsed back) — asserts ok so pyright narrows the
    union, and pins the response shape at the same time."""
    resp = await _call(daemon, tool, args, agent_id)
    assert resp["ok"] is True
    content = (resp["result"] or {}).get("content")  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(content, list) and len(content) == 1  # pyright: ignore[reportUnknownArgumentType]
    block = content[0]
    assert block["type"] == "text"
    return json.loads(block["text"])  # pyright: ignore[reportUnknownArgumentType]


# ── list_tools / ping ───────────────────────────────────────────────────────


async def test_list_tools_and_ping() -> None:
    d = _daemon()
    resp = await d._dispatch(_req("list_tools"))
    assert resp["ok"] is True
    names = {t["name"] for t in (resp["result"] or [])}
    assert names == {
        "release_control",
        "snapshot",
        "find_text",
        "click",
        "click_text",
        "type_text",
        "key",
        "scroll",
        "window_info",
        "session_info",
        "frontmost_app",
    }
    ping = await d._dispatch(_req("ping"))
    assert ping["ok"] is True
    assert ping["result"] == "pong"


async def test_unknown_method_and_tool() -> None:
    d = _daemon()
    assert (await d._dispatch(_req("nope")))["ok"] is False
    resp = await _call(d, "nope")
    assert resp["ok"] is False


# ── dispatch + execution ────────────────────────────────────────────────────


async def test_click_executes_and_converts_coordinates(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(screen_mod, "_snapshot_path", lambda _agent_id: "/tmp/x.png")  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    d = _daemon()
    resp = await _call(d, "click", {"x": 100, "y": 200})
    assert resp["ok"] is True
    # physical -> logical: divide by the 2x backing scale
    assert ("click", {"x": 50.0, "y": 100.0, "double": False}) in fake_helper.calls


async def test_snapshot_returns_path_and_geometry(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/snap-test.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    result = await _ok_result(d, "snapshot", {"include_ax": True})
    assert result["path"] == "/tmp/snap-test.png"  # noqa: S108
    assert result["screen"] == {"width": 1512.0, "height": 982.0, "scale": 2.0}
    assert result["pixels"] == {"width": 3024, "height": 1964}
    # include_ax adds the focused window geometry converted to physical
    # pixels (same click space as the PNG — 2x here).
    assert result["ax"] == {"app": "Finder", "x": 0.0, "y": 0.0, "w": 1600.0, "h": 1200.0}


async def test_snapshot_and_click_measure_scale_not_helper_claim(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (2026-08-30 probe): the helper reported scale=2 on a 1x
    display, and the daemon divided click coords by it — every click landed
    at HALF the requested position. The snapshot must measure the scale from
    the captured PNG (1920x1080 over 1920x1080 = 1), report it, and then
    click/scroll must convert with that measured value."""
    fh = fake_helper
    fh.screen = {"x": 0.0, "y": 0.0, "w": 1920.0, "h": 1080.0, "scale": 2.0}  # stale claim
    fh.png_size = (1920, 1080)
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/snap-1x.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    snap = await _ok_result(d, "snapshot", {})
    assert snap["screen"] == {"width": 1920.0, "height": 1080.0, "scale": 1.0}
    assert snap["pixels"] == {"width": 1920, "height": 1080}

    # the probe: click at (81, 15) landed on the Apple menu at (40, 7.5).
    # the physical coords must pass through UNCHANGED on a 1x display.
    await _call(d, "click", {"x": 81, "y": 15})
    assert ("click", {"x": 81.0, "y": 15.0, "double": False}) in fh.calls

    # scroll center: no re-division (1920x1080 → center 960,540).
    await _call(d, "scroll", {"x": 81, "y": 15, "dy": -5})
    assert ("scroll", {"x": 81.0, "y": 15.0, "dy": -5}) in fh.calls


async def test_snapshot_measures_scale_on_2x_and_converts_ax(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine Retina 2x capture keeps the measured scale at 2 and converts
    ax geometry into physical pixels (the click space)."""
    fh = fake_helper
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/snap-2x.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    snap = await _ok_result(d, "snapshot", {"include_ax": True})
    assert snap["screen"]["scale"] == 2.0
    assert snap["pixels"] == {"width": 3024, "height": 1964}
    assert snap["ax"]["w"] == 1600.0
    assert snap["ax"]["h"] == 1200.0

    # click AFTER the snapshot converts with the measured 2x.
    await _call(d, "click", {"x": 100, "y": 200})
    assert ("click", {"x": 50.0, "y": 100.0, "double": False}) in fh.calls


async def test_click_before_any_snapshot_falls_back_to_helper_scale(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    """Before the first snapshot the daemon has no measurement; it uses the
    helper's live screen report (also the only sane cold-start behavior)."""
    d = _daemon()
    await _call(d, "click", {"x": 100, "y": 200})
    assert ("click", {"x": 50.0, "y": 100.0, "double": False}) in fake_helper.calls


async def test_snapshot_include_ocr_adds_text_boxes(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/snap-ocr-test.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        daemon_mod.ocr_mod,
        "ocr_image",
        lambda _path: [{"text": "\u4f60\u597d", "x": 1.0, "y": 2.0, "w": 30.0, "h": 12.0}],  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    result = await _ok_result(d, "snapshot", {"include_ocr": True})
    assert result["ocr"] == [{"text": "\u4f60\u597d", "x": 1.0, "y": 2.0, "w": 30.0, "h": 12.0}]
    assert "ocr_error" not in result


async def test_snapshot_include_ocr_failure_degrades(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/snap-ocr-fail.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )

    def _boom(path: str) -> list[dict]:
        raise daemon_mod.ocr_mod.OcrError("swiftc missing")

    monkeypatch.setattr(daemon_mod.ocr_mod, "ocr_image", _boom)  # pyright: ignore[reportUnknownArgumentType]
    d = _daemon()
    result = await _ok_result(d, "snapshot", {"include_ocr": True})
    assert result["ocr"] == []
    assert result["ocr_error"] == "swiftc missing"


async def test_snapshot_without_include_ocr_skips_ocr(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/snap-plain.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    called: list[str] = []

    def _ocr(path: str) -> list[dict]:
        called.append(path)
        return []

    monkeypatch.setattr(daemon_mod.ocr_mod, "ocr_image", _ocr)  # pyright: ignore[reportUnknownArgumentType]
    d = _daemon()
    result = await _ok_result(d, "snapshot", {})
    assert called == []
    assert "ocr" not in result


# ── OCR text tools: find_text / click_text ─────────────────────────────────


class FakeOcr:
    """Scriptable ocr_image stand-in: yields canned boxes, counts runs, and
    can be switched to raise OcrError (strictness probe)."""

    def __init__(self, boxes: list[dict[str, float | str]]) -> None:
        self.boxes = boxes
        self.calls = 0
        self.error: str | None = None

    def __call__(self, path: str) -> list[dict[str, float | str]]:
        self.calls += 1
        if self.error is not None:
            raise daemon_mod.ocr_mod.OcrError(self.error)
        return self.boxes


# Physical-pixel OCR boxes over the default fake 1512x982pt @2x screen
# (3024x1964px capture): reading order is top-to-bottom, left-to-right.
_OCR_BOXES: list[dict[str, float | str]] = [
    {"text": "Hello", "x": 10.0, "y": 20.0, "w": 60.0, "h": 20.0},
    {"text": "Search", "x": 500.0, "y": 100.0, "w": 80.0, "h": 24.0},
    {"text": "search", "x": 100.0, "y": 200.0, "w": 80.0, "h": 24.0},
    {"text": "\u4f60\u597d", "x": 300.0, "y": 400.0, "w": 60.0, "h": 30.0},
    {"text": "\u4f60\u597d\u4e16\u754c", "x": 50.0, "y": 50.0, "w": 100.0, "h": 30.0},
]


@pytest.fixture
def fake_ocr(monkeypatch: pytest.MonkeyPatch) -> FakeOcr:
    stub = FakeOcr(_OCR_BOXES)
    monkeypatch.setattr(daemon_mod.ocr_mod, "ocr_image", stub)
    return stub


def _capture_count(fh: FakeHelper) -> int:
    """How many full-screen captures reached the helper."""
    return sum(1 for c in fh.calls if c[0] == "screencapture_region")


def test_match_ocr_boxes_contains_is_casefold_and_reading_order() -> None:
    """contains is a case-insensitive substring test; results come in reading
    order (top-to-bottom, then left-to-right) with a center and an index."""
    matches = ocr_text_mod._match_ocr_boxes(_OCR_BOXES, "search", "contains")
    assert [m["text"] for m in matches] == ["Search", "search"]
    assert [m["index"] for m in matches] == [0, 1]
    # "Search" box: x=500 y=100 w=80 h=24 → center (540, 112)
    assert matches[0]["cx"] == 540.0
    assert matches[0]["cy"] == 112.0
    assert matches[0]["x"] == 500.0 and matches[0]["w"] == 80.0
    # "search" box (y=200) is second
    assert matches[1]["cx"] == 140.0
    assert matches[1]["cy"] == 212.0


def test_match_ocr_boxes_exact_is_full_text_casefold() -> None:
    """exact must equal the WHOLE box text (case-insensitively) — a box that
    merely contains the query does not match."""
    needle = "\u4f60\u597d"  # "hello" in CJK
    exact = ocr_text_mod._match_ocr_boxes(_OCR_BOXES, needle, "exact")
    assert [m["text"] for m in exact] == [needle]
    contains = ocr_text_mod._match_ocr_boxes(_OCR_BOXES, needle, "contains")
    # reading order: the longer "hello world" box (y=50) precedes "hello" (y=400)
    assert [m["text"] for m in contains] == ["\u4f60\u597d\u4e16\u754c", needle]
    # exact is case-insensitive too: "hello" matches both "Hello" and "hello"
    casefolded = ocr_text_mod._match_ocr_boxes(_OCR_BOXES, "hello", "exact")
    assert [m["text"] for m in casefolded] == ["Hello"]


def test_validate_text_query_rejects_blank_and_unknown_mode() -> None:
    with pytest.raises(ComputerUseError, match="non-empty"):
        ocr_text_mod._validate_text_query("   ", "contains")
    with pytest.raises(ComputerUseError, match="'contains' or 'exact'"):
        ocr_text_mod._validate_text_query("x", "regex")


async def test_find_text_returns_matching_boxes(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """find_text captures once, OCRs, and reports every match with
    physical-pixel geometry (the click space, no scale conversion)."""
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/find-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    result = await _ok_result(d, "find_text", {"text": "search"})
    assert result["query"] == "search"
    assert result["match"] == "contains"
    assert result["fresh"] is True
    assert result["count"] == 2
    assert [m["text"] for m in result["matches"]] == ["Search", "search"]
    assert fake_ocr.calls == 1
    assert _capture_count(fake_helper) == 1


async def test_find_text_reuses_last_ocr_when_asked(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """snapshot_fresh=false searches the OCR the snapshot include_ocr just
    produced — no second capture, and the result says fresh:false."""
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/find-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    snap = await _ok_result(d, "snapshot", {"include_ocr": True})
    assert len(snap["ocr"]) == 5
    result = await _ok_result(d, "find_text", {"text": "hello", "snapshot_fresh": False})
    assert result["fresh"] is False
    assert result["count"] == 1
    assert [m["text"] for m in result["matches"]] == ["Hello"]
    assert fake_ocr.calls == 1
    assert _capture_count(fake_helper) == 1


async def test_find_text_fresh_then_stale_share_one_capture(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh find_text fills the cache; the next snapshot_fresh=false call
    searches that same screen (fresh:false) without a new capture."""
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/find-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    first = await _ok_result(d, "find_text", {"text": "search"})
    assert first["fresh"] is True
    second = await _ok_result(d, "find_text", {"text": "search", "snapshot_fresh": False})
    assert second["fresh"] is False
    assert second["count"] == 2
    assert fake_ocr.calls == 1
    assert _capture_count(fake_helper) == 1


async def test_find_text_stale_with_empty_cache_captures(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """snapshot_fresh=false before any OCR ran has nothing to reuse — it
    falls back to a fresh capture (and reports fresh:true)."""
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/find-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    result = await _ok_result(d, "find_text", {"text": "search", "snapshot_fresh": False})
    assert result["fresh"] is True
    assert result["count"] == 2
    assert fake_ocr.calls == 1
    assert _capture_count(fake_helper) == 1


async def test_find_text_ocr_failure_is_an_error(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """find_text is strict where snapshot is soft: a failed OCR is an error,
    never a silent empty list."""
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/find-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    fake_ocr.error = "swiftc missing"
    d = _daemon()
    resp = await _call(d, "find_text", {"text": "search"})
    assert resp["ok"] is False
    assert "ocr failed" in resp["error"]
    assert audit_log[0]["payload"]["outcome"] == "error"


async def test_find_text_requires_text_argument(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    d = _daemon()
    resp = await _call(d, "find_text", {})
    assert resp["ok"] is False
    assert "find_text requires argument 'text'" in resp["error"]
    resp2 = await _call(d, "find_text", {"text": "  "})
    assert resp2["ok"] is False
    assert "non-empty" in resp2["error"]


async def test_click_text_ocrs_locates_and_clicks(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """click_text = fresh capture + OCR + one click at the best match's center
    (physical pixels converted by the capture's measured 2x scale)."""
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/click-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    result = await _ok_result(d, "click_text", {"text": "search"})
    # topmost "Search" box (y=100): center (540, 112) physical → (270, 56) logical
    assert result["text"] == "Search"
    assert result["x"] == 540.0 and result["y"] == 112.0
    assert result["scale"] == 2.0
    assert ("click", {"x": 270.0, "y": 56.0, "double": False}) in fake_helper.calls
    assert fake_ocr.calls == 1
    assert _capture_count(fake_helper) == 1


async def test_click_text_index_selects_among_matches(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/click-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    # index 1 = the lower "search" box (y=200): center (140, 212) → (70, 106)
    result = await _ok_result(d, "click_text", {"text": "search", "index": 1})
    assert result["text"] == "search"
    assert ("click", {"x": 70.0, "y": 106.0, "double": False}) in fake_helper.calls
    # exact mode narrows to the full-text CJK box
    cjk = await _ok_result(d, "click_text", {"text": "\u4f60\u597d", "match": "exact"})
    assert cjk["text"] == "\u4f60\u597d"
    assert ("click", {"x": 165.0, "y": 207.5, "double": False}) in fake_helper.calls


async def test_click_text_failures_are_readable_and_never_click(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/click-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    resp = await _call(d, "click_text", {"text": "zzz"})
    assert resp["ok"] is False
    assert "no on-screen text matching 'zzz'" in resp["error"]
    resp2 = await _call(d, "click_text", {"text": "search", "index": 5})
    assert resp2["ok"] is False
    assert "out of range" in resp2["error"]
    resp3 = await _call(d, "click_text", {"text": "search", "index": -1})
    assert resp3["ok"] is False
    assert "index must be >= 0" in resp3["error"]
    resp4 = await _call(d, "click_text", {"text": "search", "match": "regex"})
    assert resp4["ok"] is False
    assert "match must be 'contains' or 'exact'" in resp4["error"]
    # none of the failures reached the helper's click
    assert not any(c[0] == "click" for c in fake_helper.calls)


async def test_click_text_audits_the_clicked_center(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/click-text.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    await _call(d, "click_text", {"text": "search"})
    actions = [ev for ev in audit_log if ev["event_type"] == "computer_action"]
    assert len(actions) == 1  # pyright: ignore[reportUnknownArgumentType]
    ev = actions[0]
    assert ev["payload"]["action"] == "click_text"
    assert ev["payload"]["outcome"] == "ok"
    # the audited coordinate is the resolved click center, physical pixels
    assert ev["payload"]["coords"] == "540.0,112.0"
    # a failed click_text audits the error with the action name, no coords
    await _call(d, "click_text", {"text": "zzz"})
    failed = [ev for ev in audit_log if ev["event_type"] == "computer_action"]
    assert failed[1]["payload"]["action"] == "click_text"
    assert failed[1]["payload"]["outcome"] == "error"
    assert failed[1]["payload"]["coords"] is None


async def test_click_text_measures_scale_and_tracks_pointer(
    fake_helper: FakeHelper,
    audit_log: list,
    fake_ocr: FakeOcr,
    monkeypatch: pytest.MonkeyPatch,
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
    d = _daemon()
    result = await _ok_result(d, "click_text", {"text": "search"})
    assert result["scale"] == 1.0
    assert ("click", {"x": 540.0, "y": 112.0, "double": False}) in fh.calls
    # scroll without coordinates follows the click_text pointer (physical
    # 540,112 → logical 540,112 on the 1x display)
    await _ok_result(d, "scroll", {"dy": -10})
    assert ("scroll", {"x": 540.0, "y": 112.0, "dy": -10}) in fh.calls
    # a plain click converts with the 1x scale click_text measured, not the
    # helper's stale 2x claim
    await _call(d, "click", {"x": 81, "y": 15})
    assert ("click", {"x": 81.0, "y": 15.0, "double": False}) in fh.calls


async def test_type_key_scroll_window_session(fake_helper: FakeHelper, audit_log: list) -> None:
    d = _daemon()
    assert (await _ok_result(d, "type_text", {"text": "\u4f60\u597d"}))["typed"] == 2
    assert (await _ok_result(d, "key", {"key": "return", "cmd": True}))["pressed"] == 36
    assert (await _ok_result(d, "scroll", {"x": 5, "y": 6, "dy": -20}))["scrolled"] == -20
    assert (await _ok_result(d, "window_info", {"owner": "Finder"}))["owner"] == "Finder"
    assert (await _ok_result(d, "session_info"))["locked"] is False


async def test_helper_failure_surfaces_as_error(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kw: Any) -> Any:
        raise PermissionsHelperError("helper down")

    monkeypatch.setattr(daemon_mod.helper, "click", _boom)
    d = _daemon()
    resp = await _call(d, "click", {"x": 1, "y": 2})
    assert resp["ok"] is False
    assert "helper down" in resp["error"]


async def test_success_result_is_call_tool_result_dump(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    """The daemon's call_tool result validates as an MCP CallToolResult —
    the exact contract the per-agent wrapper and the direct dial enforce, and
    the shape acceptance caught missing (regression for #2139)."""
    from mcp import types

    d = _daemon()
    resp = await _call(d, "frontmost_app")
    assert resp["ok"] is True
    result = types.CallToolResult.model_validate(resp["result"])
    assert result.is_error is False
    assert [b.type for b in result.content] == ["text"]
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert json.loads(block.text) == {"app": "Finder"}


async def test_missing_required_argument_fails_cleanly(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    """A missing required argument is a readable tool error, not a bare
    KeyError leaking from the helper call."""
    d = _daemon()
    resp = await _call(d, "click", {"y": 2})
    assert resp["ok"] is False
    assert "click requires argument 'x'" in resp["error"]
    assert audit_log[0]["payload"]["outcome"] == "error"
    resp2 = await _call(d, "scroll", {"x": 1, "y": 2})
    assert resp2["ok"] is False
    assert "scroll requires argument 'dy'" in resp2["error"]
    assert audit_log[1]["payload"]["outcome"] == "error"


async def test_window_info_defaults_owner_to_frontmost(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    """window_info without owner uses the frontmost app."""
    d = _daemon()
    result = await _ok_result(d, "window_info")
    assert result["owner"] == "Finder"
    assert ("window_info", {"owner": "Finder"}) in fake_helper.calls


async def test_key_accepts_names_characters_and_keycodes(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    """The key tool takes a key name, a single character, or a raw keycode."""
    d = _daemon()
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


async def test_key_unknown_name_fails_cleanly(fake_helper: FakeHelper, audit_log: list) -> None:
    d = _daemon()
    resp = await _call(d, "key", {"key": "wibble"})
    assert resp["ok"] is False
    assert "key needs a key name" in resp["error"]
    assert audit_log[0]["payload"]["outcome"] == "error"
    resp2 = await _call(d, "key")
    assert resp2["ok"] is False
    assert "key needs a key name" in resp2["error"]


async def test_key_result_maps_helper_echo_to_pressed(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    """The daemon's key response carries "pressed" even though the helper
    echoes {"key": code, "cmd": ...} — the contract callers read."""
    d = _daemon()
    result = await _ok_result(d, "key", {"key": "return", "cmd": True})
    assert result == {"pressed": 36, "cmd": True}


async def test_scroll_defaults_pointer_to_last_click(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    """scroll with only dy scrolls at the last click's position (physical
    pixels converted to logical points)."""
    d = _daemon()
    await _ok_result(d, "click", {"x": 100, "y": 200})
    result = await _ok_result(d, "scroll", {"dy": -10})
    assert result == {"scrolled": -10}
    assert ("scroll", {"x": 50.0, "y": 100.0, "dy": -10}) in fake_helper.calls


async def test_scroll_without_pointer_uses_screen_center(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    d = _daemon()
    result = await _ok_result(d, "scroll", {"dy": 5})
    assert result == {"scrolled": 5}
    # fake screen 1512x982 @2x → the logical center (756, 491) is passed
    # straight through — the old code divided it AGAIN by the scale and
    # scrolled at the upper-left quarter of the screen.
    assert ("scroll", {"x": 756.0, "y": 491.0, "dy": 5}) in fake_helper.calls


async def test_scroll_explicit_xy_updates_tracked_pointer(
    fake_helper: FakeHelper,
    audit_log: list,
) -> None:
    d = _daemon()
    await _ok_result(d, "scroll", {"x": 40, "y": 60, "dy": -5})
    result = await _ok_result(d, "scroll", {"dy": -1})
    assert result == {"scrolled": -1}
    # second scroll uses the tracked physical (40, 60) → logical (20, 30)
    assert ("scroll", {"x": 20.0, "y": 30.0, "dy": -1}) in fake_helper.calls


# ── audit stream ────────────────────────────────────────────────────────────


async def test_audit_emitted_on_success(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(screen_mod, "_snapshot_path", lambda _agent_id: "/tmp/x.png")  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    d = _daemon()
    await _call(d, "click", {"x": 100, "y": 200, "task_id": 42})
    # the computer_action row + the task-session envelope start
    actions = [ev for ev in audit_log if ev["event_type"] == "computer_action"]
    assert len(actions) == 1  # pyright: ignore[reportUnknownArgumentType]
    ev = actions[0]
    assert ev["agent_id"] == 7
    assert ev["source"] == "agent:7"
    assert ev["payload"]["action"] == "click"
    assert ev["payload"]["outcome"] == "ok"
    assert ev["payload"]["coords"] == "100,200"
    assert ev["payload"]["task_id"] == 42
    assert ev["payload"]["app"] == "Finder"
    starts = [ev for ev in audit_log if ev["event_type"] == "computer_session_start"]
    assert len(starts) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert starts[0]["payload"]["task_id"] == 42


async def test_audit_emitted_on_error(fake_helper: FakeHelper, audit_log: list) -> None:
    d = _daemon()
    await _call(d, "key", {"key": "wibble"})
    assert len(audit_log) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert audit_log[0]["payload"]["outcome"] == "error"
    assert "key needs a key name" in audit_log[0]["payload"]["error"]


async def test_no_audit_row_for_anonymous_call(audit_log: list) -> None:
    d = _daemon()
    await _call(d, "click", {"x": 1, "y": 2}, agent_id=None)
    assert audit_log == []


# ── serialization ───────────────────────────────────────────────────────────


async def test_concurrent_calls_are_safe(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent dispatches from different connections both complete and are
    both audited. Execution is synchronous and wrapped in the machine-wide
    action lock — the lock is the guard for future async points inside a call
    (e.g. Phase 2's queue), and the sync body already prevents interleaving."""
    monkeypatch.setattr(screen_mod, "_snapshot_path", lambda _a: "/tmp/x.png")  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    d = _daemon()
    t1 = asyncio.create_task(_call(d, "click", {"x": 1, "y": 2}))
    t2 = asyncio.create_task(_call(d, "snapshot"))
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert len(audit_log) == 2  # pyright: ignore[reportUnknownArgumentType]
    # both actions reached the helper, in some order
    kinds = {c[0] for c in fake_helper.calls}
    assert {"click", "screencapture_region"} <= kinds


# ── Phase 2: screen session coordination ────────────────────────────────────


def _short_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the daemon's ScreenSession milliseconds-short (fast tests)."""
    from shared.config import settings

    monkeypatch.setattr(settings.daemon, "computer_use_lease_s", 1.0)
    monkeypatch.setattr(settings.daemon, "computer_use_queue_timeout_s", 0.05)


async def test_screen_busy_blocks_second_agent(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_session(monkeypatch)
    d = _daemon()
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
) -> None:
    _short_session(monkeypatch)
    d = _daemon()
    await _call(d, "click", {"x": 1, "y": 2}, agent_id=7)
    # the holder's own next action passes through (lease renewed by the call)
    resp = await _call(d, "type_text", {"text": "hi"}, agent_id=7)
    assert resp["ok"] is True


async def test_release_control_hands_over(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_session(monkeypatch)
    d = _daemon()
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
) -> None:
    _short_session(monkeypatch)
    d = _daemon()
    await _call(d, "click", {"x": 1, "y": 2}, agent_id=7)
    resp = await _call(d, "release_control", {}, agent_id=8)
    assert resp["ok"] is False
    assert "not the screen holder" in resp["error"]


async def test_operator_force_release_without_identity(
    fake_helper: FakeHelper,
    audit_log: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_session(monkeypatch)
    d = _daemon()
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
) -> None:
    """A failing session-envelope emit (contract mismatch, FK hiccup) must not
    fail the action nor stay silent — it warns (task #1136)."""
    warnings: list[str] = []
    monkeypatch.setattr(daemon_mod.logger, "warning", lambda msg: warnings.append(str(msg)))  # pyright: ignore[reportUnknownArgumentType]
    log: list[dict] = []

    def _insert(
        *,
        event_type: str,
        agent_id: int | None,
        source: str,
        payload: dict,
        **_: object,
    ) -> None:
        if event_type.startswith("computer_session_"):
            # the envelope path is broken (unregistered name etc.)
            raise ValueError(f"unknown event name {event_type!r}")
        log.append(  # pyright: ignore[reportUnknownMemberType]
            {"event_type": event_type, "agent_id": agent_id, "source": source, "payload": payload}
        )

    monkeypatch.setattr(daemon_mod.audit_events, "insert_event_log", _insert)  # pyright: ignore[reportUnknownArgumentType]
    d = _daemon()
    resp = await _call(d, "click", {"x": 1, "y": 2, "task_id": 42})
    assert resp["ok"] is True  # the action itself executed
    assert any("task-session event failed" in w for w in warnings)
    # the computer_action row still landed (the envelope is auxiliary)
    assert [e["event_type"] for e in log] == ["computer_action"]


def _short_sock_dir() -> tuple[Path, Path, Any]:
    """A SHORT socket dir — AF_UNIX paths cap at ~104 bytes, and pytest's
    tmp_path (/private/var/folders/...) blows past it (OSError: path too long,
    which the guard treats as occupied). /tmp keeps the path short, same as
    the browser daemon tests."""
    import shutil
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="ava-cmcp-", dir="/tmp"))
    return d, d / "computer-mcp.sock", shutil.rmtree


async def test_run_raises_stream_limit_for_large_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d, sock, cleanup = _short_sock_dir()
    server_options: dict[str, Any] = {}

    async def socket_not_in_use(_path: Path) -> bool:
        return False

    async def capture_server_options(*_args: Any, **kwargs: Any) -> None:
        server_options.update(kwargs)
        raise RuntimeError("server options captured")

    monkeypatch.setattr(daemon_mod, "_socket_in_use", socket_not_in_use)
    monkeypatch.setattr(daemon_mod.asyncio, "start_unix_server", capture_server_options)

    try:
        with pytest.raises(RuntimeError, match="server options captured"):
            await daemon_mod.run(sock=str(sock))
        assert server_options["limit"] == 64 * 1024 * 1024
    finally:
        cleanup(d)


async def test_socket_in_use_false_when_nobody_listens() -> None:
    """A stale (or absent) socket is not "in use" — the daemon may unlink it."""
    d, sock, cleanup = _short_sock_dir()
    try:
        assert await daemon_mod._socket_in_use(sock) is False
    finally:
        cleanup(d)


async def test_socket_in_use_true_when_listener_present() -> None:
    """A socket with a live listener is "in use" — a second daemon must refuse
    to start instead of unlink-stealing it (the #1137 dual-daemon orphan)."""
    d, sock, cleanup = _short_sock_dir()
    server = await asyncio.start_unix_server(lambda _r, w: w.close(), path=str(sock))
    try:
        assert await daemon_mod._socket_in_use(sock) is True
    finally:
        server.close()
        await server.wait_closed()
        cleanup(d)


async def test_shutdown_cancels_active_clients() -> None:
    """run()'s shutdown path cancels tracked client handlers, so a client that
    holds its connection open cannot hang server.wait_closed() and orphan the
    daemon process (the #1137 dual-daemon root cause)."""
    d, sock, cleanup = _short_sock_dir()
    try:
        daemon = daemon_mod.ComputerMcpDaemon(sock=str(sock))
        # A client handler that never returns unless cancelled — the persistent
        # SDK connection equivalent (a real client sits in handle()'s readline).
        started = asyncio.Event()

        async def stuck_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            started.set()
            with suppress(Exception):
                await asyncio.Event().wait()  # never completes on its own

        # _tracked_client registers the done_callback that removes the task from
        # daemon._clients — the same wiring run() uses.
        task = daemon_mod._tracked_client(daemon, None, None)  # type: ignore[arg-type]
        task.cancel()  # replace the default coroutine with the stuck one below
        task = asyncio.create_task(stuck_client(None, None))  # type: ignore[arg-type]
        daemon._clients.add(task)
        task.add_done_callback(daemon._clients.discard)
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
) -> None:
    """A high-priority call queues ahead of an earlier normal one (Phase 3)."""
    from shared.config import settings

    monkeypatch.setattr(settings.daemon, "computer_use_lease_s", 1.0)
    monkeypatch.setattr(settings.daemon, "computer_use_queue_timeout_s", 0.5)
    d = _daemon()
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
) -> None:
    """A snapshot's computer_action row carries the PNG path — the trace
    replay needs it (Phase 3, task #1101)."""
    monkeypatch.setattr(
        screen_mod,
        "_snapshot_path",
        lambda _agent_id: "/tmp/snap-trace.png",  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
    )
    d = _daemon()
    await _call(d, "snapshot", {"task_id": 42})
    actions = [ev for ev in audit_log if ev["event_type"] == "computer_action"]
    assert actions[0]["payload"]["action"] == "snapshot"
    assert actions[0]["payload"]["path"] == "/tmp/snap-trace.png"  # noqa: S108
