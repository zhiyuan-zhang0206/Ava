"""`services.healthchecks.milvus` unit tests — real-RPC probe + respawn.

The probe is an application-level RPC (`MilvusClient.list_collections`), not a
TCP connect: a port-open check stays green while the server behind the port is
unusable. These tests do not start a real milvus — a fake client stands in and
the probe's contract (a real RPC must answer) is pinned.
"""

from __future__ import annotations

import pytest

from services.healthchecks import milvus as hc
from shared.config import settings


class _FakeClient:
    """Minimal stand-in for the pymilvus client surface the probe touches."""

    def __init__(self) -> None:
        self.closed = 0
        self.uri: str | None = None
        self.timeout: float | None = None

    def list_collections(self) -> list[str]:
        return []

    def close(self) -> None:
        self.closed += 1


def _patch_module(
    monkeypatch: pytest.MonkeyPatch, raise_on: str | None = None
) -> list[_FakeClient]:
    """A throwaway module standing in for pymilvus inside the probe; returns
    the fake clients the factory produced."""
    import sys
    import types

    fake = types.ModuleType("pymilvus")
    made: list[_FakeClient] = []

    def factory(*_args: object, **kwargs: object) -> _FakeClient:
        client = _FakeClient()
        uri = kwargs.get("uri")
        timeout = kwargs.get("timeout")
        client.uri = uri if isinstance(uri, str) else None
        client.timeout = timeout if isinstance(timeout, float) else None
        if raise_on == "create":
            raise OSError("connection refused")
        if raise_on == "rpc":

            def _raise_rpc() -> list[str]:
                raise OSError("milvus wedged: RPC timeout")

            client.list_collections = _raise_rpc
        made.append(client)
        return client

    fake.MilvusClient = factory  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "pymilvus", fake)
    return made


def test_is_alive_rpc_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RPC answers → alive, and the client is closed after the probe."""
    made = _patch_module(monkeypatch)
    assert hc._is_alive() is True
    assert len(made) == 1
    assert made[0].uri == settings.services.milvus_uri
    assert made[0].timeout == hc._TIMEOUT_S
    assert made[0].closed == 1


def test_is_alive_client_create_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server unreachable (client create raises) → dead, no crash."""
    _patch_module(monkeypatch, raise_on="create")
    assert hc._is_alive() is False


def test_is_alive_rpc_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression this probe exists for: a process holds the port but the
    RPC fails (wedged server / foreign occupant) → dead, so the watchdog
    respawns instead of certifying a broken milvus forever."""
    _patch_module(monkeypatch, raise_on="rpc")
    assert hc._is_alive() is False


def test_restart_invokes_respawn_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_restart_daemon` uses `respawn_service` (same pattern as the other
    daemon healthchecks)."""
    calls: list[tuple[str, str]] = []
    extra_envs: list[dict[str, str]] = []

    def fake_respawn(session: str, cmd: str, _repo: object, **kwargs: object) -> bool:
        calls.append((session, cmd))
        env: object = kwargs.get("extra_env", {})
        extra_envs.append(env if isinstance(env, dict) else {})  # pyright: ignore[reportUnknownArgumentType]
        return True

    monkeypatch.setattr(hc, "respawn_service", fake_respawn)
    ok = hc._restart_daemon()
    assert ok is True
    assert calls == [("milvus", ".venv/bin/python -m services.milvus.daemon")]
    assert extra_envs == [{"AVA_PROCESS_PROFILE": "gateway"}]
