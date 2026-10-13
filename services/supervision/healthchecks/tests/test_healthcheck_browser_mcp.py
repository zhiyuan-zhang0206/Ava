"""ava-browser-mcp healthcheck — a probe returns a bool, never an exception."""

import json
import logging

import pytest

import services.supervision.healthchecks.browser_mcp as hc


def test_alive_true_when_probe_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "browser_mcp_probe", lambda: True)
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

    monkeypatch.setattr(hc, "browser_mcp_probe", _boom)
    assert hc._is_alive() is False


def test_alive_false_on_any_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ANY unforeseen probe failure degrades to "dead",
    with a traceback so nothing is swallowed silently."""

    def _boom() -> bool:
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(hc, "browser_mcp_probe", _boom)
    with caplog.at_level(logging.ERROR, logger="services.supervision.healthchecks.browser_mcp"):
        assert hc._is_alive() is False
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)
