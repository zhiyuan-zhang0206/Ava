"""`services.im_bridge.daemon` liveness wiring.

The im_bridge main loop never iterates once the adapters are launched (it parks
on ``asyncio.Event().wait()``), so the healthz liveness must be carried by a
dedicated background task — without it, ``/healthz`` flips to 503 two minutes
after startup and the watchdog respawns a perfectly healthy daemon in a loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from typing import Any, cast

import pytest

from base.config import get_field, settings
from base.daemon.health import Liveness
from services.im_bridge import daemon
from services.im_bridge.config import (
    FeishuCredentialsConfig,
    ImBridgeConfig,
    TelegramCredentialsConfig,
)
from services.im_bridge.gateway_client import GatewayClient


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

    send_auth: list[frozenset[str] | None] = []

    async def fake_start_health_server(
        _name: str,
        _port: int,
        *,
        liveness: Liveness | None = None,
        auth_digests: frozenset[str] | None = None,
        **_kwargs: Any,
    ) -> _FakeServer:
        assert liveness is not None
        captured.append(liveness)
        send_auth.append(auth_digests)
        return _FakeServer()

    # /send accepts the machine API tokens of the write generation, never the human secret.
    monkeypatch.setattr(daemon, "daemon_acceptance", lambda: frozenset({"digest-of-a-token"}))

    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "start_health_server", fake_start_health_server)

    # run() imports IMBridgeCore inside the function; patch the module it
    # imports from, not the daemon module itself.
    created_cores: list[Any] = []

    class _FakeCore:
        def __init__(self, config: ImBridgeConfig, gateway: Any, db_pool: Any = None) -> None:
            self.config = config
            self.gateway = gateway
            self.db_pool = db_pool
            self.outbox_replay_started = False
            created_cores.append(self)

        async def restore_subscriptions(self) -> None:
            pass

        def ensure_outbox_replay(self) -> None:
            self.outbox_replay_started = True

    monkeypatch.setattr("services.im_bridge.core.IMBridgeCore", _FakeCore)

    loaded_with: list[frozenset[str]] = []

    def fake_load_adapters(_core: object, disabled: frozenset[str]) -> list[object]:
        loaded_with.append(disabled)
        return []

    monkeypatch.setattr(daemon, "_load_adapters", fake_load_adapters)
    monkeypatch.setattr(daemon, "_LIVENESS_BEAT_INTERVAL_S", 0.02)

    async def scenario() -> None:
        task = asyncio.create_task(daemon.run())
        try:
            await asyncio.sleep(0.5)
            assert captured, "run() never started the health server"
            assert captured[0].is_alive()
            assert send_auth == [frozenset({"digest-of-a-token"})]
            assert created_cores[0].outbox_replay_started  # Task #1032: drain on startup
            # The root builds the slice once and hands the same one to every consumer.
            assert created_cores[0].config == daemon.im_bridge_config()
            assert isinstance(created_cores[0].gateway, GatewayClient)
            assert loaded_with == [frozenset(created_cores[0].config.im_disabled_adapters)]
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_load_adapters_skips_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_IM_DISABLED_ADAPTERS skips the named adapters at load; the code
    stays importable (user ruling 2026-08-06: only Telegram stays live)."""
    imported: list[str] = []

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        mod = name.rsplit(".", 1)[-1]
        imported.append(mod)

        class _FakeAdapter:
            def __init__(self, core: Any, *config: Any) -> None:
                pass

        return type("mod", (), {"ADAPTER_CLASS": _FakeAdapter})

    monkeypatch.setattr(daemon, "_import_adapter", fake_import)

    class _FakeCore:
        def __init__(self) -> None:
            self.registered: list[Any] = []

        def register(self, adapter: Any) -> None:
            self.registered.append(adapter)

    core = _FakeCore()
    loaded = daemon._load_adapters(core, frozenset({"weixin", "feishu"}))
    assert imported == ["telegram"]
    assert len(loaded) == 1
    assert len(core.registered) == 1


def test_load_adapters_hands_each_adapter_its_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The root passes the Telegram and Feishu adapters their own slice; Weixin reads none."""
    received: dict[str, tuple[Any, ...]] = {}

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        mod = name.rsplit(".", 1)[-1]

        class _FakeAdapter:
            def __init__(self, core: Any, *config: Any) -> None:
                received[mod] = config

        return type("mod", (), {"ADAPTER_CLASS": _FakeAdapter})

    monkeypatch.setattr(daemon, "_import_adapter", fake_import)

    class _FakeCore:
        def register(self, adapter: Any) -> None:
            pass

    daemon._load_adapters(_FakeCore(), frozenset())
    assert [type(c) for c in received["telegram"]] == [TelegramCredentialsConfig]
    assert received["weixin"] == ()
    assert [type(c) for c in received["feishu"]] == [FeishuCredentialsConfig]


@pytest.mark.parametrize(
    "build",
    [daemon.im_bridge_config, daemon.telegram_config, daemon.feishu_config],
)
def test_a_built_slice_carries_the_live_value_of_every_field(
    build: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each slice field is the flat registry field of the same name, so an operator's
    `.env` edit reaches the component through the root, and no field is left at a default."""
    monkeypatch.setattr(settings.services, "im_disabled_adapters", ["weixin"])
    monkeypatch.setattr(settings.services, "im_send_retry_delays", [0.5, 1.5])
    monkeypatch.setattr(settings.telegram, "telegram_owner_id", 4242)
    monkeypatch.setattr(settings.feishu, "feishu_app_id", "cli_x")
    config = build()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        expected = tuple(cast(list[Any], flat)) if isinstance(flat, list) else flat
        assert getattr(config, field.name) == expected


def test_the_gateway_client_gets_the_gateway_url_and_bearer_from_the_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon, "gateway_auth_headers", lambda: {"Authorization": "Bearer t"})
    client = daemon.gateway_client(daemon.im_bridge_config())
    assert client._base == settings.gateway.gateway_url.rstrip("/")
    assert client._headers() == {"Authorization": "Bearer t"}


def test_httpx_info_logs_gated(caplog: pytest.LogCaptureFixture) -> None:
    """Telegram's bot token rides in the request URL, so httpx's per-request
    INFO line must not be emitted at all once the daemon gates it; library
    warnings still surface (task #4067)."""
    httpx_logger = logging.getLogger("httpx")
    previous_level = httpx_logger.level
    caplog.set_level(logging.INFO)
    try:
        # Control: ungated, the URL-bearing INFO line does reach the log —
        # the pre-fix state this test exists to keep out.
        httpx_logger.setLevel(logging.NOTSET)
        caplog.clear()
        httpx_logger.info(
            "HTTP Request: GET https://api.telegram.org/bot1234567890:FAKE-TOKEN/getUpdates"
        )
        assert "FAKE-TOKEN" in caplog.text

        daemon._gate_httpx_info_logs()
        assert httpx_logger.getEffectiveLevel() == logging.WARNING
        caplog.clear()
        httpx_logger.info(
            "HTTP Request: GET https://api.telegram.org/bot1234567890:FAKE-TOKEN/getUpdates"
        )
        httpx_logger.warning("connection pool is full, discarding connection")

        assert "FAKE-TOKEN" not in caplog.text
        assert "connection pool is full" in caplog.text
    finally:
        httpx_logger.setLevel(previous_level)
