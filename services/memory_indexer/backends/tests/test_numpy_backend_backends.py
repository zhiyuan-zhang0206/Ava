"""The backend preflight probes: an unreachable service is transient and names the fix, an unknown backend is fatal."""

from __future__ import annotations

import socket

import pytest

from base.db import Database
from services.memory_indexer.backends import probe


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_probe_numpy_unreachable_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down service is transient (may be booting) but the message must name
    the fix and the switch action."""
    from base.config import settings

    monkeypatch.setattr(settings.services, "memory_search_uri", f"http://127.0.0.1:{_free_port()}")
    result = probe.probe_backend("numpy", Database.from_settings())
    assert not result.fatal  # booting is transient — the retry loop owns the wait
    assert "memory_search service is not reachable" in (result.message or "")
    assert "AVA_MEMORY_SEARCH_BACKEND=numpy" in (result.message or "")


def test_probe_unknown_backend_is_fatal() -> None:
    result = probe.probe_backend("qdrant", Database.from_settings())
    assert result.fatal
    assert "unknown memory search backend" in (result.message or "")
