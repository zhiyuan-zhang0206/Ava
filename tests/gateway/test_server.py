"""Gateway process-entry invariants."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from gateway import _server
from shared.config import settings


def test_gateway_pins_uvicorn_to_one_worker_for_process_local_rate_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Gateway rate-limit state has one authority, so startup fixes uvicorn at one worker."""
    captured: dict[str, object] = {}

    def _ignore(*_args: object, **_kwargs: object) -> None:
        return None

    def _record_run(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(settings.services, "gateway_pidfile", tmp_path / "gateway.pid")
    monkeypatch.setattr(_server, "assert_schema_current", _ignore)
    monkeypatch.setattr(_server, "raise_fd_limit", _ignore)
    monkeypatch.setattr(_server, "init_gateway_process", _ignore)
    monkeypatch.setattr(_server, "is_gateway", lambda: False)
    monkeypatch.setattr(_server.faulthandler, "register", _ignore)
    monkeypatch.setattr(_server.uvicorn, "run", _record_run)

    with caplog.at_level(logging.WARNING, logger="gateway._server"):
        _server.main()

    assert captured["workers"] == 1
    assert "rate limit" in caplog.text
