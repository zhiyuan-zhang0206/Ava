"""Attachment roots own DB admission, borrowed clients and failed-construction rollback."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Self, cast
from unittest.mock import patch

import pytest

import ava
from ava import external
from ava.sdk_surface import settings as _settings
from base.agents.impersonation import manifest
from base.agents.impersonation.event_log import LOG_PROTOCOL_VERSION
from base.config import ConfigBoot
from base.db import Database, code_version_gate, connections
from tests.factories.external_attachment import attached_runtime as attached_runtime


class _RestoreCursor:
    """A cursor closes independently from its caller's connection."""

    def __init__(self, connection: _AttachmentDial) -> None:
        self.connection = connection

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def execute(self, sql: str, _params: object = None) -> None:
        self.connection.statements.append(sql)

    def fetchone(self) -> tuple[str, str, int]:
        return ("60000", "attachment", 0)


class _AttachmentDial:
    """Observe the real session restore while keeping this contract off a server."""

    def __init__(self) -> None:
        self.closed = False
        self.broken = False
        self.statements: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def execute(self, sql: str, _params: object = None) -> Self:
        self.statements.append(sql)
        return self

    def cursor(self, **_kwargs: object) -> _RestoreCursor:
        return _RestoreCursor(self)

    def fetchone(self) -> tuple[str, str, int]:
        return ("60000", "attachment", 0)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_independent_attachments_check_the_minimum_on_first_borrow_and_close_own_clients(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second attachment starts fresh even inside the first one's thirty seconds."""
    now = [100.0]
    dials: list[_AttachmentDial] = []
    require = external.control.require_active
    prior = ava.unbind_context()

    def dial(*_args: object, **_kwargs: object) -> _AttachmentDial:
        connection = _AttachmentDial()
        dials.append(connection)
        return connection

    def active(db: Database, lease_id: str, attesting: dict[str, Any]) -> dict[str, Any]:
        with db.connect(autocommit=True):
            pass
        return require(db, lease_id, attesting)

    def legacy_check() -> bool:
        raise AssertionError("an attachment must not read the module-level check timestamp")

    monkeypatch.setattr(connections.psycopg, "connect", dial)
    monkeypatch.setattr(code_version_gate.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(connections, "min_read_due", legacy_check)
    monkeypatch.setattr(external.control, "require_active", active)
    (tmp_path / ".env").write_text(
        "AVA_DB_URL=postgresql://test:test@127.0.0.1:1/attachment\n"
        "AVA_REDIS_URL=redis://127.0.0.1:1/0\n"
        "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n",
        encoding="utf-8",
    )
    try:
        with patch.dict(os.environ, {"AVA_HOME": str(tmp_path), "AVA_CONFIG_FETCH": "skip"}):
            writers_before = {
                thread for thread in threading.enumerate() if thread.name == "event-emitter"
            }
            for timestamp in (100.0, 101.0):
                now[0] = timestamp
                first_dial = len(dials)
                with external.attach("lease"):
                    first_db = _settings.database()
                    second_db = _settings.database()
                    assert first_db is not second_db
                    first_db.connect(autocommit=True).close()
                    second_db.connect(autocommit=True).close()
                    ava.context.sql.execute("SELECT 1")
                    live = dials[-1]
                    assert not live.closed
                    current = dials[first_dial:]
                    assert (
                        sum(
                            "min_code_version" in sql
                            for connection in current
                            for sql in connection.statements
                        )
                        == 1
                    )
                assert live.closed
                assert {
                    thread for thread in threading.enumerate() if thread.name == "event-emitter"
                } == writers_before
                assert getattr(ava, "context", None) is None
    finally:
        if prior is not None:
            ava.bind_context(prior)


def test_attachment_borrows_existing_database_factory_without_closing_prior_clients(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    prior = ava.context
    handles: list[Database] = []

    def original() -> Database:
        return database

    def borrowed_database() -> Database:
        handle = original()
        handles.append(handle)
        return handle

    monkeypatch.setattr(prior.clients, "database", borrowed_database)
    prior.sql.execute("SELECT 1")
    original_connection = cast(Any, prior.sql._get())
    with external.attach("lease"):
        assert ava.context.clients is prior.clients
        assert _settings.database() in handles
    assert ava.context is prior
    assert prior.sql._get() is original_connection
    assert not original_connection.closed


@pytest.mark.parametrize("failure_at", ["lease", "plugins", "snapshot"])
def test_unbound_attachment_failure_closes_owned_clients_and_releases_binding(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_at: str,
) -> None:
    prior = ava.unbind_context()
    created: list[tuple[Any, list[bool]]] = []
    build = external.process_context.process_clients
    primary = RuntimeError("owned constructor failed")

    def clients(**kwargs: Any) -> Any:
        result = build(**kwargs)
        close = result.close
        was_closed = [False]

        def close_owned() -> None:
            was_closed[0] = True
            close()

        monkeypatch.setattr(result, "close", close_owned)
        created.append((result, was_closed))
        return result

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise primary

    monkeypatch.setattr(external.process_context, "process_clients", clients)
    target = (
        (external.control, "require_active")
        if failure_at == "lease"
        else (ava, "ensure_plugins_loaded")
        if failure_at == "plugins"
        else (external, "load_snapshot")
    )
    (tmp_path / ".env").write_text(
        "AVA_DB_URL=postgresql://test:test@127.0.0.1:1/attachment\n"
        "AVA_REDIS_URL=redis://127.0.0.1:1/0\n"
        "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n",
        encoding="utf-8",
    )
    try:
        with patch.dict(os.environ, {"AVA_HOME": str(tmp_path), "AVA_CONFIG_FETCH": "skip"}):
            with monkeypatch.context() as failed:
                failed.setattr(*target, fail)
                with pytest.raises(RuntimeError) as raised:
                    external.attach("lease")
                assert raised.value is primary
            assert created[0][1][0]
            assert getattr(ava, "context", None) is None
            with external.attach("lease"):
                assert ava.self.AGENT_ID == 405
            assert created[1][1][0]
    finally:
        if prior is not None:
            ava.bind_context(prior)


def test_independent_attachment_seal_wait_reads_its_live_configuration_owner(
    attached_runtime: tuple[dict[str, Any], Any, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lease, _, _ = attached_runtime
    lease.update(automatic=True, event_delivery_protocol_version=LOG_PROTOCOL_VERSION)
    owners: list[ConfigBoot] = []
    waits: list[float] = []

    def build() -> ConfigBoot:
        owner = ConfigBoot()
        owners.append(owner)
        return owner

    def wait(_gate: manifest.LocalCaptureGate, *, timeout: float) -> bool:
        waits.append(timeout)
        return False  # Pending work retains its own receipt; this probe does not seal it.

    def open_participant(_db: Database, _lease_id: str, *, agent_id: int, source_key: str) -> bool:
        return True

    monkeypatch.setattr(external, "ConfigBoot", build)
    monkeypatch.setattr(manifest, "open_local_participant", open_participant)
    monkeypatch.setattr(manifest, "close_local_participant_admission", wait)
    (tmp_path / ".env").write_text("AVA_MACHINE_SERVE_AGENT_RUNNER=true\n", encoding="utf-8")
    prior = ava.unbind_context()
    try:
        with patch.dict(os.environ, {"AVA_HOME": str(tmp_path), "AVA_CONFIG_FETCH": "skip"}):
            attachment = external.attach("lease")
            assert len(owners) == 1
            owners[0].set_field("impersonation_event_seal_wait_seconds", 37)
            assert waits == []
            attachment.close()
            assert waits == [37]
            assert getattr(ava, "context", None) is None
    finally:
        if prior is not None:
            ava.bind_context(prior)
