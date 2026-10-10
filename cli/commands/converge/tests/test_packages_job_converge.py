"""Converge registers the recurring packages-refresh OS job."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.config import ConfigBoot
from base.host.system import packages_job as job
from cli.commands.converge.spec import ConvergeCtx


def test_converge_registers_the_refresh_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.commands.converge._os_jobs import ensure_packages_refresh_job

    calls: list[str] = []

    def register(
        *, enabled_reader: object, refresh_enabled_reader: object, tick_reader: object
    ) -> None:
        calls.append("register")

    monkeypatch.setattr(job, "register_packages_job", register)

    ctx = ConvergeCtx(repo=tmp_path, ava_home=tmp_path, roles=None, config=ConfigBoot())
    ensure_packages_refresh_job(ctx)
    assert calls == ["register"]
