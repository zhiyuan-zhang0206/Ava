"""meiva-mcp-daemon healthcheck — a probe returns a bool, never an exception."""

import json
import logging
from pathlib import Path

import pytest

import services.supervision.healthchecks.mcp_daemon as hc


def test_alive_true_when_probe_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hc, "mcp_daemon_probe", lambda _home: True)  # pyright: ignore[reportUnknownArgumentType]
    assert hc.is_alive(tmp_path) is True


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
    monkeypatch: pytest.MonkeyPatch, exc: Exception, tmp_path: Path
) -> None:
    """The transport failures that mean "the daemon is down" stay quiet."""

    def _boom(_home: Path) -> bool:
        raise exc

    monkeypatch.setattr(hc, "mcp_daemon_probe", _boom)
    assert hc.is_alive(tmp_path) is False


def test_alive_false_on_any_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Any unforeseen probe failure degrades to "dead", with a traceback so
    nothing is swallowed silently."""

    def _boom(_home: Path) -> bool:
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(hc, "mcp_daemon_probe", _boom)
    with caplog.at_level(logging.ERROR, logger="services.supervision.healthchecks.mcp_daemon"):
        assert hc.is_alive(tmp_path) is False
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)
