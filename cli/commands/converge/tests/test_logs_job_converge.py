"""Converge registers the daily logs-maintenance OS job."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.system import cron
from base.host.system import logs_job as job


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/work tree/.venv/bin/ava")
    monkeypatch.setattr(cron, "job_home", lambda: "/home/u/.ava")
    monkeypatch.setattr(cron, "launchd_path_env", lambda: "/work tree/.venv/bin:/usr/bin")


def test_converge_registers_logs_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands.converge._os_jobs import ensure_logs_maintenance

    calls: list[str] = []
    monkeypatch.setattr(job, "register_logs_job", lambda: calls.append("register"))

    ensure_logs_maintenance(None)  # type: ignore[arg-type]
    assert calls == ["register"]
