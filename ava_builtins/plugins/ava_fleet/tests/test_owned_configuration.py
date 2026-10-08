"""Fleet owns its policy; bootstrap and service birth snapshots keep each scope."""

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from ava_builtins.plugins.ava_fleet.default_config import FleetConfig, contribute
from ava_builtins.plugins.ava_fleet.services import services
from base.config.service_read import plugin_bootstrap_config
from base.host.env import bootstrap, runtime_config
from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV, SERVICE_PLUGIN_CONFIG_ENV
from base.packages.plugins.config_registration import (
    InvalidConfigData,
    SchemaDriftError,
    disk_image_path,
    read_plugin_config,
    read_service_config,
    service_config_packet,
)
from base.packages.plugins.flags import UndeclaredFlag, read_declared_flag


@pytest.fixture(autouse=True)
def preserve_env_file() -> Generator[None]:
    env = runtime_config.env_file_path()
    previous = env.read_bytes() if env.exists() else None
    try:
        yield
    finally:
        if previous is None:
            env.unlink(missing_ok=True)
        else:
            env.write_bytes(previous)


def write_image(config: FleetConfig) -> Path:
    path = disk_image_path("ava_fleet")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.model_dump_json())
    return path


def test_remote_cluster_projection_never_overrides_local_host_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = write_image(FleetConfig(task_maintenance_enabled=False, task_escalate_n=99))
    gateway = FleetConfig(task_escalate_n=7, reduce_context_switch=False).model_dump(mode="json")
    gateway.pop("task_maintenance_enabled")
    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: False)
    monkeypatch.setattr(bootstrap, "should_fetch_from_gateway", lambda: True)
    monkeypatch.setenv(PLUGIN_CLUSTER_CONFIG_ENV, json.dumps({"ava_fleet": gateway}))
    actual = read_plugin_config("ava_fleet", FleetConfig, image)
    assert actual.task_maintenance_enabled is False
    assert actual.task_escalate_n == 7
    assert actual.reduce_context_switch is False
    monkeypatch.delenv(PLUGIN_CLUSTER_CONFIG_ENV)
    with pytest.raises(InvalidConfigData, match="lacks cluster"):
        read_plugin_config("ava_fleet", FleetConfig, image)


@pytest.mark.parametrize("packet", ['{"ava_fleet":null}', '{"ava_fleet":[]}', "[]"])
def test_invalid_present_projection_is_rejected_even_on_gateway(
    packet: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: True)
    monkeypatch.setenv(PLUGIN_CLUSTER_CONFIG_ENV, packet)
    with pytest.raises(TypeError):
        read_plugin_config("ava_fleet", FleetConfig, write_image(FleetConfig()))


def test_gateway_truth_is_authoritative_and_projection_has_exact_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = write_image(FleetConfig(task_escalate_n=7, task_maintenance_enabled=False))
    packet = json.loads(plugin_bootstrap_config())["ava_fleet"]
    assert set(packet) == set(FleetConfig.model_fields) - {"task_maintenance_enabled"}
    assert packet["task_escalate_n"] == 7
    packet["task_escalate_n"] = 99
    monkeypatch.setenv(PLUGIN_CLUSTER_CONFIG_ENV, json.dumps({"ava_fleet": packet}))
    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: True)
    assert read_plugin_config("ava_fleet", FleetConfig, image).task_escalate_n == 7
    packet["task_escalate_n"] = "invalid"
    monkeypatch.setenv(PLUGIN_CLUSTER_CONFIG_ENV, json.dumps({"ava_fleet": packet}))
    with pytest.raises(InvalidConfigData):
        read_plugin_config("ava_fleet", FleetConfig, image)
    packet.pop("task_escalate_n")
    monkeypatch.setenv(PLUGIN_CLUSTER_CONFIG_ENV, json.dumps({"ava_fleet": packet}))
    with pytest.raises(SchemaDriftError):
        read_plugin_config("ava_fleet", FleetConfig, image)


def test_gate_and_daemon_birth_share_one_captured_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = write_image(FleetConfig(task_maintenance_enabled=False, task_escalate_n=7))
    service = services()[0]
    assert service.config_inputs == (image,)
    assert service.gate is not None and "disabled" in str(service.gate())
    assert service.plugin_config is not None
    name, captured = service.plugin_config
    monkeypatch.setenv(SERVICE_PLUGIN_CONFIG_ENV, service_config_packet(name, captured))
    write_image(FleetConfig(task_maintenance_enabled=True, task_escalate_n=99))
    actual = read_service_config("ava_fleet", FleetConfig)
    assert actual is not None and actual.task_escalate_n == 7
    assert actual.task_maintenance_enabled is False
    assert "disabled" in str(service.gate())


def test_legacy_pending_aborts_service_roster_without_default_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.packages.plugins import enable_config, load_report
    from ops.spec import plugin_services

    folder = Path(__file__).parents[1]
    monkeypatch.setattr(enable_config, "installed_plugin_dirs", lambda: {"ava_fleet": folder})
    errors: list[str] = []

    def capture(_name: str, error: BaseException) -> None:
        errors.append(str(error))

    monkeypatch.setattr(load_report, "report_plugin_load_failure", capture)
    disk_image_path("ava_fleet").unlink(missing_ok=True)
    runtime_config.env_file_path().write_text("AVA_TASK_MAINTENANCE_ENABLED=false\n")
    with pytest.raises(InvalidConfigData, match="plugins update"):
        plugin_services()
    assert len(errors) == 1 and "plugins update" in errors[0]
    assert not disk_image_path("ava_fleet").exists()
    with pytest.raises(InvalidConfigData, match="plugins update"):
        plugin_bootstrap_config()


def test_core_dependency_is_declared_and_validated_without_sdk_install() -> None:
    assert contribute().flags == ("daemon.notice_ttl_limit_seconds",)
    assert isinstance(read_declared_flag(contribute().flags[0], contribute().flags), float)
    with pytest.raises(UndeclaredFlag):
        read_declared_flag("daemon.notice_ttl_limit_seconds", ())
