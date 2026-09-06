"""ava-browser-mcp healthcheck — a probe returns a bool, never an exception.

The regression this locks: `_probe` names `socket.AF_UNIX`, which does not exist
on Windows, so it raised `AttributeError` — a type the old
`except (OSError, json.JSONDecodeError)` did not catch. The watchdog isolates
each check, so the round survived, but browser-mcp was never judged alive or
dead (no restart was ever attempted) and every 60s round wrote a multi-KB
traceback. Measured on the `win` runner: `healthcheck browser-mcp raised`, 20+
consecutive rounds, in a 50 MB watchdog log.

The service is now gated off where AF_UNIX is absent (`ops.spec._gate_reason`),
so these cover the second line of defence rather than the live path.
"""

import json
import logging
import socket

import pytest

import services.healthchecks.browser_mcp as hc


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


def test_alive_false_on_missing_af_unix(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The Windows shape, reproduced on any platform by deleting the attribute
    `_probe` names. This must return False, not propagate — the healthcheck owes
    the watchdog a verdict."""
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    with caplog.at_level(logging.ERROR, logger="services.healthchecks.browser_mcp"):
        assert hc._is_alive() is False
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)


def test_alive_false_on_any_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Not just AttributeError: ANY unforeseen probe failure degrades to "dead",
    with a traceback so nothing is swallowed silently."""

    def _boom() -> bool:
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(hc, "_probe", _boom)
    with caplog.at_level(logging.ERROR, logger="services.healthchecks.browser_mcp"):
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
