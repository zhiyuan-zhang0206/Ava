"""`services.healthchecks.otel_collector` unit tests — OTLP probe + respawn.

The sidecar's health is "does the OTLP receiver answer" — ANY HTTP status from
/v1/traces proves the collector's listener is up (same probe the agent
exporters use at init). These tests verify the probe logic and the respawn
invocation without running a collector.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from services.healthchecks import otel_collector as hc


def test_is_alive_any_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTPError (415 for a probe the collector rejects) still proves the
    listener answered — alive."""

    def _raise(_req, **_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise urllib.error.HTTPError(
            "http://127.0.0.1:4318/v1/traces",
            415,
            "Unsupported Media Type",
            {},  # pyright: ignore[reportArgumentType]
            None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert hc._is_alive() is True


def test_is_alive_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 proves the receiver parsed the probe — alive."""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda _req, **_kw: _Resp())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    assert hc._is_alive() is True


def test_is_alive_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection-level failure — the sidecar is down."""

    def _raise(_req, **_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._is_alive() is False


def test_restart_invokes_respawn_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_restart_daemon` respawns the collector with the roster's command."""

    def fake_respawn(session: str, cmd: str, _repo, **kwargs) -> bool:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        assert session == "otel-collector"
        assert "otelcol-contrib" in cmd and "config.yaml" in cmd
        return True

    monkeypatch.setattr(hc, "respawn_service", fake_respawn)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._restart_daemon() is True
