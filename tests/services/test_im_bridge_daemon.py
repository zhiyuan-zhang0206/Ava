"""`services.im_bridge.daemon` liveness wiring.

The im_bridge main loop never iterates once the adapters are launched (it parks
on ``asyncio.Event().wait()``), so the healthz liveness must be carried by a
dedicated background task — without it, ``/healthz`` flips to 503 two minutes
after startup and the watchdog respawns a perfectly healthy daemon in a loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from services.im_bridge import daemon
from shared.daemon_health import Liveness


class _FakeServer:
    """Minimal stand-in for the asyncio.Server ``stop_health_server`` touches."""

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def test_liveness_loop_beats_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    """The beat task keeps a Liveness fresh — the regression guard for the
    503-after-startup bug."""
    monkeypatch.setattr(daemon, "_LIVENESS_BEAT_INTERVAL_S", 0.02)
    liveness = Liveness(timeout_s=0.2)

    async def scenario() -> None:
        task = asyncio.create_task(daemon._liveness_loop(liveness))
        try:
            await asyncio.sleep(0.5)  # ~25 beats, several times the timeout
            assert liveness.is_alive()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_run_wires_the_liveness_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() hands the health server a Liveness that keeps getting beaten: the
    beat task is actually created and running, not merely defined."""
    captured: list[Liveness] = []

    async def fake_start_health_server(
        _name: str, *, liveness: Liveness | None = None, **_kwargs: Any
    ) -> _FakeServer:
        assert liveness is not None
        captured.append(liveness)
        return _FakeServer()

    async def fake_login(_core: Any, _liveness: Liveness) -> None:
        pass

    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "start_health_server", fake_start_health_server)

    # run() imports IMBridgeCore inside the function; patch the module it
    # imports from, not the daemon module itself.
    created_cores: list[Any] = []

    class _FakeCore:
        def __init__(self, db_pool: Any = None) -> None:
            self.db_pool = db_pool
            self.outbox_replay_started = False
            created_cores.append(self)

        async def restore_subscriptions(self) -> None:
            pass

        def ensure_outbox_replay(self) -> None:
            self.outbox_replay_started = True

    monkeypatch.setattr("services.im_bridge.core.IMBridgeCore", _FakeCore)

    def fake_load_adapters(_core: object) -> list[object]:
        return []

    monkeypatch.setattr(daemon, "_load_adapters", fake_load_adapters)
    monkeypatch.setattr(daemon, "_gateway_login_with_retry", fake_login)
    monkeypatch.setattr(daemon, "_LIVENESS_BEAT_INTERVAL_S", 0.02)

    async def scenario() -> None:
        task = asyncio.create_task(daemon.run())
        try:
            await asyncio.sleep(0.5)
            assert captured, "run() never started the health server"
            assert captured[0].is_alive()
            assert created_cores[0].outbox_replay_started  # Task #1032: drain on startup
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_load_adapters_skips_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_IM_DISABLED_ADAPTERS skips the named adapters at load; the code
    stays importable (user ruling 2026-08-06: only Telegram stays live)."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "im_disabled_adapters", ["weixin", "feishu"])
    imported: list[str] = []

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        mod = name.rsplit(".", 1)[-1]
        imported.append(mod)

        class _FakeAdapter:
            def __init__(self, core: Any) -> None:
                pass

        return type("mod", (), {"ADAPTER_CLASS": _FakeAdapter})

    monkeypatch.setattr(daemon, "_import_adapter", fake_import)

    class _FakeCore:
        def __init__(self) -> None:
            self.registered: list[Any] = []

        def register(self, adapter: Any) -> None:
            self.registered.append(adapter)

    core = _FakeCore()
    loaded = daemon._load_adapters(core)
    assert imported == ["telegram"]
    assert len(loaded) == 1
    assert len(core.registered) == 1
