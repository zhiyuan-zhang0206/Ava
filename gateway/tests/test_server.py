"""Gateway process-entry invariants."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from base.config import settings
from base.db import Database
from gateway.cluster import server as _server


def test_gateway_pins_uvicorn_to_one_worker_for_process_local_rate_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Gateway rate-limit state has one authority, so startup fixes uvicorn at one worker."""
    captured: dict[str, object] = {}

    def _ignore(*_args: object, **_kwargs: object) -> None:
        return None

    def _record_serve(kwargs: dict[str, object]) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(settings.services, "gateway_pidfile", tmp_path / "gateway.pid")
    monkeypatch.setattr(_server, "assert_schema_current", _ignore)
    monkeypatch.setattr(_server, "raise_fd_limit", _ignore)
    monkeypatch.setattr(_server, "init_gateway_process", _ignore)
    monkeypatch.setattr(_server, "is_gateway", lambda: False)
    monkeypatch.setattr(_server.faulthandler, "register", _ignore)
    monkeypatch.setattr(_server, "serve", _record_serve)

    with caplog.at_level(logging.WARNING, logger="gateway.cluster.server"):
        _server.main()

    assert captured["workers"] == 1
    assert "rate limit" in caplog.text


def test_gateway_launch_bounds_the_connection_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launch assembly must hand uvicorn a finite graceful-shutdown budget.

    The 2026-09-17 gateway stall: uvicorn's connection-drain phase has no
    default timeout, so an unfinished streaming response held it forever. The
    real child-process regression lives in
    gateway/tests/test_server_shutdown.py; this pins the value the assembly
    resolves for uvicorn.
    """
    monkeypatch.setattr(settings.gateway, "gateway_graceful_shutdown_timeout_seconds", 7.5)
    kwargs = _server.serve_kwargs(host="127.0.0.1")
    assert kwargs["timeout_graceful_shutdown"] == 7.5


@pytest.mark.parametrize(("serves_gateway", "raised"), [(True, 1), (False, 0)])
def test_gateway_start_raises_the_min_code_version_after_schema_and_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serves_gateway: bool, raised: int
) -> None:
    """A gateway start raises the cluster's minimum code version, once the schema
    is asserted (the column exists) and logging is up (a refusal is recorded); a
    runner's local gateway holds no write on `deployment_state` and never does."""
    steps: list[str] = []

    def _ignore(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(settings.services, "gateway_pidfile", tmp_path / "gateway.pid")

    def _schema_asserted(_url: str) -> None:
        steps.append("schema")

    def _raised(_db: Database) -> int:
        steps.append("raise")
        return 1

    monkeypatch.setattr(_server, "assert_schema_current", _schema_asserted)
    monkeypatch.setattr(_server, "raise_fd_limit", _ignore)
    monkeypatch.setattr(_server, "init_gateway_process", lambda: steps.append("logging"))
    monkeypatch.setattr(_server, "raise_min_code_version", _raised)
    monkeypatch.setattr(_server, "is_gateway", lambda: serves_gateway)
    monkeypatch.setattr(_server, "verify_transport_encryption", _ignore)
    monkeypatch.setattr(_server.faulthandler, "register", _ignore)
    monkeypatch.setattr(_server, "serve", _ignore)

    _server.main()

    assert steps.count("raise") == raised
    if raised:
        assert steps == ["schema", "logging", "raise"]
