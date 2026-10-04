"""`_supervise_handle`'s failed-relaunch severity contract (2026-10-03, E5).

A wedged page session whose shell cannot run the server command is torn down
and rebuilt in the same reconcile pass — the failed relaunch is a step of the
self-healing path, so its line is WARNING, never ERROR. This module exists
beside `test_page_server_daemon.py` because that file sits at its frozen size
ceiling.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import cast

import pytest

from base.sessions.backend import SessionBackend
from services.agent_runner.page_server import daemon as psd
from services.agent_runner.page_server.degradation import _PageRow


class _Backend:
    """A session whose shell transport cannot run the server command."""

    def __init__(self) -> None:
        self.killed: list[str] = []

    def send(self, _session: str, _command: str) -> None:
        raise RuntimeError("pty session host is not answering")

    def kill_session(self, session: str) -> None:
        self.killed.append(session)


def test_failed_relaunch_logs_warning_and_tears_the_session_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.WARNING, logger="services.agent_runner.page_server.daemon")

    def _unhealthy(*_args: object) -> bool:
        return False

    def _no_port(*_args: object) -> None:
        return None

    monkeypatch.setattr(psd, "_server_is_healthy", _unhealthy)
    monkeypatch.setattr(psd, "_probe_port", _no_port)
    session = "ava-agent-42-shell-3-page-wedged"
    serve_dir = str(tmp_path / "page")
    token = secrets.token_hex(8)
    row = _PageRow(
        id=1,
        agent_id=42,
        name="wedged",
        port=12016,
        host="test-host",
        serve_dir=serve_dir,
        server_token=token,
        session_name=session,
    )
    key: tuple[int, str] = (42, "wedged")
    handle = psd._ServerHandle(42, "wedged", 12016, serve_dir, token, session, 0.0)
    managed: dict[tuple[int, str], psd._ServerHandle] = {key: handle}
    backend = _Backend()

    tore_down = psd._supervise_handle(
        cast("SessionBackend", backend), row, key, handle, managed, {}, {}, {}, 1e9
    )

    assert tore_down is True, "the caller recreates the session in this same pass"
    assert backend.killed == [session]
    assert key not in managed
    assert [
        record.levelname for record in caplog.records if "relaunch failed" in record.getMessage()
    ] == ["WARNING"]
