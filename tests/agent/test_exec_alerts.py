"""P2 #2102: the bootstrap-class exec-failure alert — rate-limited, threaded,
never raising, Alertmanager-shaped for the gateway's /api/alerts ingest."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent.graph import _exec_alerts
from agent.graph._exec_result import ExecChildError


class _InlineThread:
    """Runs the thread target synchronously so tests see the POST deterministically."""

    def __init__(
        self,
        target: Callable[..., object],
        args: tuple[object, ...],
        name: str,
        daemon: bool,
    ) -> None:
        del name, daemon
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


@pytest.fixture(autouse=True)
def _reset_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_exec_alerts, "_last_posted_at", None)
    monkeypatch.setattr(_exec_alerts.threading, "Thread", _InlineThread)  # pyright: ignore[reportUnknownArgumentType]


def test_alert_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.machine.machine_name", lambda: "test-box")  # pyright: ignore[reportUnknownArgumentType]
    payload = _exec_alerts._payload(7, "BootstrapFetchError", "gateway down")
    alert = payload["alerts"][0]
    assert payload["source"] == "agent-exec"
    assert alert["status"] == "firing"
    assert alert["labels"]["alertname"] == "exec child boot failed"
    assert alert["labels"]["severity"] == "warning"
    assert alert["labels"]["agent"] == "7"
    assert alert["labels"]["machine"] == "test-box"
    assert "BootstrapFetchError" in alert["annotations"]["summary"]
    assert "gateway down" in alert["annotations"]["summary"]


def test_one_alert_per_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(_exec_alerts.time, "monotonic", lambda: clock["t"])  # pyright: ignore[reportUnknownArgumentType]
    posted: list[tuple[int, str, str]] = []
    monkeypatch.setattr(_exec_alerts, "_post", lambda a, t, m: posted.append((a, t, m)))  # pyright: ignore[reportUnknownArgumentType]

    exc = ExecChildError("BootstrapFetchError", "down", None)
    _exec_alerts.maybe_alert_exec_boot_failure(7, exc)
    _exec_alerts.maybe_alert_exec_boot_failure(7, exc)  # inside the window: dropped
    assert posted == [(7, "BootstrapFetchError", "down")]

    clock["t"] = 601.0  # window elapsed: a fresh alert fires
    _exec_alerts.maybe_alert_exec_boot_failure(7, exc)
    assert len(posted) == 2


def test_post_swallows_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("unreachable")

    monkeypatch.setattr(httpx, "post", _boom)
    _exec_alerts._post(7, "BootstrapFetchError", "down")  # must not raise


def test_post_uses_bearer_and_gateway_base(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.config.settings.data_plane.cluster_secret", "the-secret")
    monkeypatch.setattr("shared.machine.machine_name", lambda: "test-box")  # pyright: ignore[reportUnknownArgumentType]

    class _Resp:
        status_code = 200

    def _fake_post(
        url: str, *, json: dict[str, object], headers: dict[str, str], timeout: float
    ) -> object:
        calls.append((url, json))
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    _exec_alerts._post(7, "BootstrapFetchError", "down")
    assert calls[0][0] == "http://gw:8000/api/alerts"
