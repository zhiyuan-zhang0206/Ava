"""Offline Darwin rendering inputs shared by the native and station test groups."""

from __future__ import annotations

from pathlib import Path

import pytest

from base import config
from base.packages import dashboard_supply
from cli.commands.observability import lgtm_native
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

__all__ = ["darwin_rendering", "default_provisioning", "empty_native_versions"]

_STUB_RENDER = '{"title": "Ava Ops", "panels": []}\n'


def _skip_binary_verification(_home: Path) -> None:
    """Render fixtures do not install native executable assets."""


@pytest.fixture(autouse=True)
def darwin_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    """These existing lifecycle cases exercise the Darwin implementation."""
    monkeypatch.setattr(lgtm_native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(lgtm_native.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(lgtm_native, "verify_loki", _skip_binary_verification)


@pytest.fixture(autouse=True)
def default_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-render assertions use an explicit default deployment configuration."""
    monkeypatch.setattr(
        config.settings.data_plane, "db_url", "postgresql://reader@127.0.0.1:5433/ava"
    )
    monkeypatch.setattr(config.settings.gateway, "gateway_url", "")
    monkeypatch.setattr(config.settings.gateway, "gateway_port", 8000)

    def render_dashboard_json(_db: object, _repo_only: bool = False) -> tuple[str, tuple[str, ...]]:
        return _STUB_RENDER, ()

    monkeypatch.setattr(
        dashboard_supply,
        "render_dashboard_json",
        render_dashboard_json,
    )


def empty_native_versions(_repo: Path) -> dict[str, dict[str, str]]:
    return {}
