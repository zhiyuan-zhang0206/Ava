"""The native daemon fixes SDK process posture before any boot dependency read."""

from __future__ import annotations

import os
from unittest.mock import Mock, patch

import pytest

import ava
from agent.extensions import load_extensions
from ava.sdk_surface import install
from base import config
from services.agent_runner.agent_host import daemon
from services.agent_runner.agent_host.lifecycle.configuration import load_installation


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
    with patch.dict(os.environ):
        boot = config.ConfigBoot()
        boot.boot()
        try:
            producer = Mock(
                side_effect=AssertionError("plugin loading must keep its producer cold")
            )
            installation = load_installation(
                boot, producer=producer, load_extensions=load_extensions
            )
            assert installation.require_catalog().models
            assert installation.producer is producer
            producer.assert_not_called()
            assert installation.authority is not None
            assert installation.authority.runtime is boot.view
            assert installation.authority.all_domains.profile is None
            assert installation.authority.all_domains is boot.view
        finally:
            install.uninstall()


def test_profiled_host_defers_complete_model_until_first_service_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with patch.dict(os.environ):
        _assert_profiled_host_defers_complete_model(monkeypatch)


def _assert_profiled_host_defers_complete_model(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_type = config.Settings
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    boot = config.ConfigBoot()
    boot.boot()
    runtime = boot.view
    builds: list[str | None] = []

    def build(*, profile: str | None) -> config.Settings:
        builds.append(profile)
        return settings_type(profile=profile)

    monkeypatch.setattr(config, "Settings", build)
    try:
        producer = Mock(side_effect=AssertionError("plugin loading must keep its producer cold"))
        installation = load_installation(boot, producer=producer, load_extensions=load_extensions)
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
