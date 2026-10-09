"""The native daemon fixes SDK process posture before any boot dependency read."""

from __future__ import annotations

import pytest

import ava
from ava.sdk_surface import install
from base import config
from services.agent_runner.agent_host import daemon


def test_sdk_posture_precedes_config_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    class BootStoppedError(RuntimeError):
        pass

    reads: list[str] = []

    def fix_posture() -> None:
        reads.append("sdk_posture")
        raise BootStoppedError("stop before boot")

    monkeypatch.setattr(ava, "bind_host_process", fix_posture)
    monkeypatch.setattr(config, "ensure_eager", lambda: reads.append("config"))
    with pytest.raises(BootStoppedError, match="before boot"):
        daemon.main()
    assert reads == ["sdk_posture"]


def test_host_root_supplies_model_and_config_owners() -> None:
    try:
        installation = daemon._load_plugin_installation()
        assert installation.require_catalog().models
        assert installation.authority is not None
        assert installation.authority.runtime is daemon.settings
        assert installation.authority.all_domains.profile is None
        assert installation.authority.all_domains is daemon.settings
    finally:
        install.uninstall()


def test_profiled_host_defers_complete_model_until_first_service_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_type = config.Settings
    runtime = settings_type(profile="agent")
    builds: list[str | None] = []

    def build(*, profile: str | None) -> config.Settings:
        builds.append(profile)
        return settings_type(profile=profile)

    monkeypatch.setattr(daemon, "settings", runtime)
    monkeypatch.setattr(config, "Settings", build)
    try:
        installation = daemon._load_plugin_installation()
        authority = installation.authority
        assert authority is not None
        assert authority.runtime is runtime
        assert builds == []
        assert authority.service_field_value("machine_host") == runtime.general.machine_host
        assert builds == []
        first = authority.service_field_value("provider_guard_balance_min_cny")
        assert builds == [None]
        assert authority.service_field_value("provider_guard_balance_min_cny") == first
        assert builds == [None]
    finally:
        install.uninstall()
