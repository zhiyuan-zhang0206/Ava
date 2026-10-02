"""P2 #2102: the bootstrap-class exec-failure alert — rate-limited, threaded,
never raising, Alertmanager-shaped for the gateway's /api/alerts ingest."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent.graph.exec import _alerts
from agent.graph.exec._result import ExecChildError


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
    monkeypatch.setattr(_alerts, "_last_posted_at", {})
    monkeypatch.setattr(_alerts.threading, "Thread", _InlineThread)  # pyright: ignore[reportUnknownArgumentType]


def test_alert_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "test-box")  # pyright: ignore[reportUnknownArgumentType]
    payload = _alerts._payload(7, "BootstrapFetchError", "gateway down")
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
    monkeypatch.setattr(_alerts.time, "monotonic", lambda: clock["t"])  # pyright: ignore[reportUnknownArgumentType]
    posted: list[tuple[int, str, str]] = []
    monkeypatch.setattr(_alerts, "_post", lambda a, t, m: posted.append((a, t, m)))  # pyright: ignore[reportUnknownArgumentType]

    exc = ExecChildError("BootstrapFetchError", "down", None)
    _alerts.maybe_alert_exec_boot_failure(7, exc)
    _alerts.maybe_alert_exec_boot_failure(7, exc)  # inside the window: dropped
    assert posted == [(7, "BootstrapFetchError", "down")]

    clock["t"] = 601.0  # window elapsed: a fresh alert fires
    _alerts.maybe_alert_exec_boot_failure(7, exc)
    assert len(posted) == 2


def _record_posted_agents(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the POST with a recorder of the agent ids that reached it."""
    posted: list[int] = []

    def _post(agent_id: int, _exc_type: str, _exc_msg: str) -> None:
        posted.append(agent_id)

    monkeypatch.setattr(_alerts, "_post", _post)
    return posted


def test_agents_do_not_suppress_each_others_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host serves many agents: agent 7's alert must not hide agent 8's for
    the window, while each agent is still limited on its own."""
    clock = {"t": 0.0}
    monkeypatch.setattr(_alerts.time, "monotonic", lambda: clock["t"])  # pyright: ignore[reportUnknownArgumentType]
    posted = _record_posted_agents(monkeypatch)

    exc = ExecChildError("BootstrapFetchError", "down", None)
    _alerts.maybe_alert_exec_boot_failure(7, exc)
    clock["t"] = 10.0
    _alerts.maybe_alert_exec_boot_failure(8, exc)  # another agent, inside 7's window
    _alerts.maybe_alert_exec_boot_failure(7, exc)  # same agent, inside its window: dropped
    _alerts.maybe_alert_exec_boot_failure(8, exc)  # same agent, inside its window: dropped
    assert posted == [7, 8]

    clock["t"] = 601.0  # 7's window elapsed, 8's (opened at t=10) has not
    _alerts.maybe_alert_exec_boot_failure(7, exc)
    _alerts.maybe_alert_exec_boot_failure(8, exc)
    assert posted == [7, 8, 7]


def test_expired_stamps_are_dropped_so_the_map_stays_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(_alerts.time, "monotonic", lambda: clock["t"])  # pyright: ignore[reportUnknownArgumentType]
    posted = _record_posted_agents(monkeypatch)

    exc = ExecChildError("BootstrapFetchError", "down", None)
    for agent_id in range(1, 6):
        _alerts.maybe_alert_exec_boot_failure(agent_id, exc)
    assert posted == [1, 2, 3, 4, 5]
    assert set(_alerts._last_posted_at) == {1, 2, 3, 4, 5}

    clock["t"] = 601.0  # every stamp has expired; the next call sweeps them
    _alerts.maybe_alert_exec_boot_failure(9, exc)
    assert posted[-1] == 9
    assert set(_alerts._last_posted_at) == {9}


def test_post_swallows_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("unreachable")

    monkeypatch.setattr(httpx, "post", _boom)
    _alerts._post(7, "BootstrapFetchError", "down")  # must not raise


def test_post_uses_bearer_and_gateway_base(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.config.settings.data_plane.cluster_secret", "the-secret")
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "test-box")  # pyright: ignore[reportUnknownArgumentType]

    class _Resp:
        status_code = 200

    def _fake_post(
        url: str, *, json: dict[str, object], headers: dict[str, str], timeout: float
    ) -> object:
        calls.append((url, json))
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    _alerts._post(7, "BootstrapFetchError", "down")
    assert calls[0][0] == "http://gw:8000/api/alerts"
