"""Converge registers the daily logs-maintenance OS job."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.config import ConfigBoot
from base.host.system import logs_job as job
from cli.commands.converge.spec import ConvergeCtx


def test_converge_registers_logs_maintenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.commands.converge._os_jobs import ensure_logs_maintenance

    calls: list[str] = []

    def register(*, enabled_reader: object) -> None:
        calls.append("register")

    monkeypatch.setattr(job, "register_logs_job", register)

    ctx = ConvergeCtx(repo=tmp_path, ava_home=tmp_path, roles=None, config=ConfigBoot())
    ensure_logs_maintenance(ctx)
    assert calls == ["register"]
