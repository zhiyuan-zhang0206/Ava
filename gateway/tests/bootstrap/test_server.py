"""Gateway process-entry invariants."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from base.cluster.auth import ClusterSecretMissingError
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from gateway.cluster import server as _server
from tests.fixtures.configuration import snapshot_process_config


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
    monkeypatch.setattr(_server, "ConfigBoot", snapshot_process_config)
    monkeypatch.setattr(_server, "assert_schema_current", _ignore)
    monkeypatch.setattr(_server, "raise_fd_limit", _ignore)
    monkeypatch.setattr(_server, "init_gateway_process", _ignore)
    monkeypatch.setattr(settings.general, "machine_serve_gateway", False)
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
    gateway/tests/bootstrap/test_server_shutdown.py; this pins the value the assembly
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

    def _raised(_gate: ProcessDbGate, _db: Database) -> int:
        steps.append("raise")
        return 1

    monkeypatch.setattr(_server, "ConfigBoot", snapshot_process_config)
    monkeypatch.setattr(_server, "assert_schema_current", _schema_asserted)
    monkeypatch.setattr(_server, "raise_fd_limit", _ignore)

    def logging_initialized(**_inputs: object) -> None:
        steps.append("logging")

    monkeypatch.setattr(_server, "init_gateway_process", logging_initialized)
    monkeypatch.setattr(ProcessDbGate, "raise_min_code_version", _raised)
    monkeypatch.setattr(settings.general, "machine_serve_gateway", serves_gateway)
    monkeypatch.setattr(_server, "verify_transport_encryption", _ignore)
    monkeypatch.setattr(_server.faulthandler, "register", _ignore)
    monkeypatch.setattr(_server, "serve", _ignore)

    _server.main()

    assert steps.count("raise") == raised
    if raised:
        assert steps == ["schema", "logging", "raise"]


def _stub_process_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, gateway: bool
) -> list[str]:
    """Stub everything `main()` does besides choosing its secret check and bind host."""
    steps: list[str] = []

    def _ignore(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(settings.services, "gateway_pidfile", tmp_path / "gateway.pid")

    def _schema(_url: str) -> None:
        steps.append("schema")

    def _serve(kwargs: dict[str, object]) -> None:
        steps.append(f"serve:{kwargs['host']}")

    monkeypatch.setattr(_server, "ConfigBoot", snapshot_process_config)
    monkeypatch.setattr(_server, "assert_schema_current", _schema)
    monkeypatch.setattr(_server, "raise_fd_limit", _ignore)
    monkeypatch.setattr(_server, "init_gateway_process", _ignore)
    monkeypatch.setattr(ProcessDbGate, "raise_min_code_version", _ignore)
    monkeypatch.setattr(settings.general, "machine_serve_gateway", gateway)
    monkeypatch.setattr(_server, "verify_transport_encryption", _ignore)
    monkeypatch.setattr(_server.faulthandler, "register", _ignore)
    monkeypatch.setattr(_server, "serve", _serve)
    return steps


def test_a_gateway_without_a_cluster_secret_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API always authenticates: no secret, no gateway. The refusal comes first,
    before the database is touched or a pidfile written."""
    steps = _stub_process_entry(monkeypatch, tmp_path, gateway=True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")

    with pytest.raises(ClusterSecretMissingError, match="AVA_CLUSTER_SECRET"):
        _server.main()

    assert steps == []
    assert not (tmp_path / "gateway.pid").exists()


@pytest.mark.parametrize(
    ("reachable", "gateway", "bind"),
    [
        ("localhost", True, "127.0.0.1"),  # the single-box default: loopback, secret or not
        ("10.0.0.5", True, ""),  # a declared private-network address: all interfaces
        ("10.0.0.5", False, "127.0.0.1"),  # a runner's local gateway never faces the LAN
    ],
)
def test_gateway_bind_follows_the_declared_reachable_address_not_the_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reachable: str,
    gateway: bool,
    bind: str,
) -> None:
    """The secret is always set; reach is `AVA_MACHINE_HOST`'s. A secret-bearing gateway
    with a non-loopback address binds exactly as it always has (all interfaces)."""
    steps = _stub_process_entry(monkeypatch, tmp_path, gateway=gateway)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s3cret")
    monkeypatch.setattr(settings.general, "machine_host", reachable)

    _server.main()

    assert steps[-1] == f"serve:{bind}"
