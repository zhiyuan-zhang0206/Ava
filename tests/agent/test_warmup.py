"""`agent.warmup` unit tests.

The heavy warm steps (import chain, MCP daemon spawn, browser connect) are
monkeypatched out — the contract under test is: `main()` runs each step
best-effort and always returns 0, browser warm is gated on `browser_enabled` and
`browser_incapability()` (skipping with a logged reason when incapable), and the
CDP probe is non-raising.
"""

from __future__ import annotations

import pytest

from agent import warmup
from shared.config import settings


def _stub_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two always-run steps so main() does no real work."""
    monkeypatch.setattr(warmup, "_warm_imports", lambda: None)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(warmup, "_warm_mcp_daemon", _noop)


def test_main_returns_zero_even_when_steps_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every step is best-effort: main() returns 0 even if all of them blow up."""

    def _boom() -> None:
        raise RuntimeError("import boom")

    async def _aboom() -> None:
        raise RuntimeError("daemon boom")

    monkeypatch.setattr(warmup, "_warm_imports", _boom)
    monkeypatch.setattr(warmup, "_warm_mcp_daemon", _aboom)
    monkeypatch.setattr(settings.services, "browser_enabled", False)

    assert warmup.main() == 0


def test_main_skips_browser_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_core(monkeypatch)
    called: list = []

    async def _browser() -> None:
        called.append(True)  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr(warmup, "_warm_browser", _browser)
    monkeypatch.setattr(settings.services, "browser_enabled", False)

    assert warmup.main() == 0
    assert called == []


def test_main_runs_browser_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_core(monkeypatch)
    called: list = []

    async def _browser() -> None:
        called.append(True)  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr(warmup, "_warm_browser", _browser)
    monkeypatch.setattr(settings.services, "browser_enabled", True)
    monkeypatch.setattr(warmup, "browser_incapability", lambda: None)

    assert warmup.main() == 0
    assert called == [True]


def test_main_skips_browser_when_incapable_and_logs_reason(
    monkeypatch: pytest.MonkeyPatch, loguru_records
) -> None:
    """browser_enabled=True but incapable: _warm_browser is NOT called, and the
    reason is logged (the bug was a silent skip with zero signal). warmup logs via
    loguru, so this uses the loguru_records bridge rather than caplog."""
    _stub_core(monkeypatch)
    called: list = []

    async def _browser() -> None:
        called.append(True)  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr(warmup, "_warm_browser", _browser)
    monkeypatch.setattr(settings.services, "browser_enabled", True)
    monkeypatch.setattr(warmup, "browser_incapability", lambda: "no display (headless)")

    assert warmup.main() == 0
    assert called == []
    assert any(
        "browser warm skipped" in r["message"] and "no display" in r["message"]
        for r in loguru_records
    )


def test_cdp_live_false_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(warmup.urllib.request, "urlopen", _boom)  # pyright: ignore[reportUnknownArgumentType]
    assert warmup._cdp_live(9222) is False


def test_cdp_live_true_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(warmup.urllib.request, "urlopen", lambda *_a, **_kw: _Resp())  # pyright: ignore[reportUnknownArgumentType]
    assert warmup._cdp_live(9222) is True


async def test_warm_browser_skips_when_cdp_never_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """When CDP never answers, _warm_browser returns without connecting."""
    monkeypatch.setattr(warmup, "_cdp_live", lambda _port: False)  # pyright: ignore[reportUnknownArgumentType]
    # deadline 0 → the poll loop body never runs, no real sleeping
    monkeypatch.setattr(settings.sandbox, "mcp_daemon_start_timeout_seconds", 0.0)

    import ava._mcps_daemon as daemon

    def _must_not_connect(*_a, **_kw):
        raise AssertionError("must not connect when CDP is down")

    monkeypatch.setattr(daemon, "_connect_server", _must_not_connect)  # pyright: ignore[reportUnknownArgumentType]

    await warmup._warm_browser()  # returns cleanly
