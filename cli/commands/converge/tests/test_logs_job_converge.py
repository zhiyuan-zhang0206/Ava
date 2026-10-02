"""Converge registers the daily logs-maintenance OS job."""

from __future__ import annotations

import pytest

from base.host.system import logs_job as job


def test_converge_registers_logs_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands.converge._os_jobs import ensure_logs_maintenance

    calls: list[str] = []
    monkeypatch.setattr(job, "register_logs_job", lambda: calls.append("register"))

    ensure_logs_maintenance(None)  # type: ignore[arg-type]
    assert calls == ["register"]
