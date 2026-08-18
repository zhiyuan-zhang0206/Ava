"""`services.healthchecks.milvus` unit tests — TCP probe + Popen restart.

The milvus standalone server does not expose HTTP /healthz; uses TCP connect for health check
(gRPC listening = ready). These tests do not actually start milvus — only verify the health-check
logic and restart invocation.
"""

from __future__ import annotations

import socket

import pytest

from services.healthchecks import milvus as hc


def test_is_alive_tcp_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP connect succeeds → alive."""

    class _FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(socket, "create_connection", lambda _addr, **_kw: _FakeSock())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert hc._is_alive() is True


def test_is_alive_tcp_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP connect refused / OSError → dead."""

    def _raise(_addr, **_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", _raise)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._is_alive() is False


def test_restart_invokes_respawn_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_restart_daemon` uses `respawn_service` (same pattern as the 5 daemon healthchecks in #257)."""
    calls: list[tuple[str, str]] = []
    extra_envs: list[dict[str, str]] = []

    def fake_respawn(session: str, cmd: str, _repo, **kwargs) -> bool:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        calls.append((session, cmd))
        extra_envs.append(kwargs.get("extra_env", {}))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return True

    monkeypatch.setattr(hc, "respawn_service", fake_respawn)  # pyright: ignore[reportUnknownArgumentType]
    ok = hc._restart_daemon()
    assert ok is True
    assert calls == [("milvus", ".venv/bin/python -m services.milvus.daemon")]
    assert extra_envs == [{"AVA_PROCESS_PROFILE": "gateway"}]
