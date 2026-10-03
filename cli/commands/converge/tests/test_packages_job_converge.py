"""Converge registers the recurring packages-refresh OS job."""

from __future__ import annotations

import pytest

from base.host.system import packages_job as job


def test_converge_registers_the_refresh_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands.converge._os_jobs import ensure_packages_refresh_job

    calls: list[str] = []
    monkeypatch.setattr(job, "register_packages_job", lambda: calls.append("register"))

    ensure_packages_refresh_job(None)  # type: ignore[arg-type]
    assert calls == ["register"]
