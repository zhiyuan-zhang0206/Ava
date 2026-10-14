"""Offline Darwin rendering inputs shared by the native and station test groups."""

from __future__ import annotations

from pathlib import Path

import pytest

from base import config
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, FakeCommands, Reply, fake_host
from base.native_process.os_platform import Platform
from base.packages import dashboard_supply
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

__all__ = ["darwin_host", "default_provisioning", "empty_native_versions", "verifying_host"]

_STUB_RENDER = '{"title": "Ava Ops", "panels": []}\n'


def verifying_host(platform: Platform) -> Host:
    """A host on `platform` whose pinned Loki parser answers that the config verifies.

    Render fixtures install no native executable, so nothing else is runnable: any other
    command fails the test.
    """
    commands = FakeCommands()
    commands.on(lambda argv: "-verify-config" in argv, Reply(), repeat=True)
    return fake_host(commands, platform=platform)


@pytest.fixture
def darwin_host() -> Host:
    """The Darwin implementation these lifecycle cases exercise."""
    return verifying_host(DARWIN_ARM64)


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


def empty_native_versions(_repo: Path, *, platform: Platform) -> dict[str, dict[str, str]]:
    return {}
