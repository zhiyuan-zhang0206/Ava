"""Converge registers the recurring packages-refresh OS job."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.system import cron
from base.host.system import packages_job as job


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/work tree/.venv/bin/ava")
    monkeypatch.setattr(cron, "job_home", lambda: str(tmp_path / ".ava"))
    monkeypatch.setattr(cron, "launchd_path_env", lambda: "/work tree/.venv/bin:/usr/bin")


def test_converge_registers_the_refresh_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands.converge._os_jobs import ensure_packages_refresh_job

    calls: list[str] = []
    monkeypatch.setattr(job, "register_packages_job", lambda: calls.append("register"))

    ensure_packages_refresh_job(None)  # type: ignore[arg-type]
    assert calls == ["register"]
