"""ava-mcp-daemon healthcheck — a probe returns a bool, never an exception.

The regression this locks: `_probe` names `socket.AF_UNIX`, which does not exist
on Windows. The daemon binds that very socket, so on such a host the service
cannot run at all — it is gated out of the ops roster (`ops.spec._gate_reason`)
and the watchdog never schedules this check. Before the gate, the win runner's
watchdog judged the daemon dead every 60s (the healthcheck's broad except
degraded the probe failure to "dead"), attempted a restart that could never
succeed, and logged an ERROR each round — measured 1,257 ERRORs/24h with the
mcp health monitoring effectively blind.

The explicit no-AF_UNIX branch in `_is_alive` is the second line of defence
(a manual run, or a future gate regression): it no-ops as alive instead of
walking dead -> restart against a service that can never start.
"""

import json
import logging

import pytest

import services.healthchecks.mcp_daemon as hc


def test_alive_true_when_probe_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "_probe", lambda: True)
    assert hc._is_alive() is True


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("no listener"),
        FileNotFoundError("socket absent"),
        TimeoutError("daemon wedged"),
        json.JSONDecodeError("bad", "", 0),
    ],
    ids=["refused", "no-socket", "timeout", "bad-json"],
)
def test_alive_false_on_expected_probe_failures(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """The transport failures that mean "the daemon is down" stay quiet."""

    def _boom() -> bool:
        raise exc

    monkeypatch.setattr(hc, "_probe", _boom)
    assert hc._is_alive() is False


def test_alive_true_without_af_unix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows shape: no AF_UNIX, so no probe is even attempted — the
    healthcheck no-ops as alive (the service is gated out; a restart could
    never succeed)."""
    called: list[bool] = []

    def _probe() -> bool:
        called.append(True)
        return False

    monkeypatch.setattr(hc, "unix_sockets_available", lambda: False)
    monkeypatch.setattr(hc, "_probe", _probe)
    assert hc._is_alive() is True
    assert called == []


def test_alive_false_on_any_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Any unforeseen probe failure degrades to "dead", with a traceback so
    nothing is swallowed silently."""

    def _boom() -> bool:
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(hc, "_probe", _boom)
    with caplog.at_level(logging.ERROR, logger="services.healthchecks.mcp_daemon"):
        assert hc._is_alive() is False
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)


def test_main_does_not_propagate_an_unexpected_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the module's entrypoint reaches its restart decision instead
    of unwinding into the watchdog's exception handler."""

    def _boom() -> bool:
        raise AttributeError("module 'socket' has no attribute 'AF_UNIX'")

    restarts: list[str] = []

    def _restart() -> bool:
        restarts.append("restart")
        return True

    monkeypatch.setattr(hc, "_probe", _boom)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_restart_daemon", _restart)
    hc.main()
    assert restarts == ["restart"]
