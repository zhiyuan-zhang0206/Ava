"""Owned boot values and fresh file reads have separate, explicit lifetimes."""

from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from base.config import Settings
from base.config.service_read import ConfigAuthority
from base.host.env.config_registry import field_alias


def _complete(*, db_user: str = "ava", machine_host: str = "") -> Settings:
    return Settings(
        profile=None,
        data_plane={
            "db_url": f"postgresql://{db_user}@127.0.0.1:5433/ava",
            "redis_url": "redis://127.0.0.1:6379/0",
        },
        general={"machine_host": machine_host},
    )


def _authority(path: Path, *, profile: str | None = None) -> ConfigAuthority:
    complete = _complete()
    runtime = complete if profile is None else complete.model_copy(update={"profile": profile})
    return ConfigAuthority(runtime=runtime, all_domains=complete, env_path=path)


def test_two_authorities_read_their_own_files_and_boot_models(tmp_path: Path) -> None:
    one_path, two_path = tmp_path / "one.env", tmp_path / "two.env"
    one_path.write_text("AVA_TRACE_ENABLED=false\n")
    two_path.write_text("AVA_TRACE_ENABLED=true\n")
    one, two = _authority(one_path), _authority(two_path)
    two.runtime.sandbox.exec_timeout_seconds = 42

    assert one.current_field_values()["trace_enabled"] is False
    assert two.current_field_values()["trace_enabled"] is True
    assert one.current_field_values()["exec_timeout_seconds"] != 42
    assert two.current_field_values()["exec_timeout_seconds"] == 42

    one_path.write_text("AVA_TRACE_ENABLED=true\n")
    assert one.current_field_values()["trace_enabled"] is True
    assert two.current_field_values()["trace_enabled"] is True


def test_fresh_reads_preserve_boot_snapshot_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    authority = _authority(path)
    initial = authority.flat_dump()["delivery_outbox_enabled"]
    path.write_text(f"{field_alias('delivery_outbox_enabled')}={'false' if initial else 'true'}\n")
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "another-home"))

    assert authority.current_field_values()["delivery_outbox_enabled"] is not initial
    assert authority.flat_dump()["delivery_outbox_enabled"] is initial
    assert authority.runtime.daemon.delivery_outbox_enabled is initial

    path.write_text(f"{field_alias('delivery_outbox_enabled')}={'true' if initial else 'false'}\n")
    assert authority.current_field_values()["delivery_outbox_enabled"] is initial


def test_service_reads_are_complete_and_runtime_profile_remains_restricted(tmp_path: Path) -> None:
    authority = _authority(tmp_path / ".env", profile="gateway")
    authority.runtime.__dict__.pop("sandbox")

    with pytest.raises(AttributeError, match="sandbox"):
        _ = authority.runtime.sandbox
    assert (
        authority.current_field_values()["exec_timeout_seconds"]
        == authority.all_domains.sandbox.exec_timeout_seconds
    )
    assert (
        authority.flat_dump()["exec_timeout_seconds"]
        == authority.all_domains.sandbox.exec_timeout_seconds
    )


def test_unexpected_domain_attribute_error_is_not_recovered(tmp_path: Path) -> None:
    authority = _authority(tmp_path / ".env")
    authority.runtime.__dict__.pop("sandbox")

    with pytest.raises(AttributeError, match="sandbox"):
        authority.service_field_value("exec_timeout_seconds")


@pytest.mark.parametrize(
    "line", ["AVA_TRACE_ENABLED=banana\n", "AVA_IM_SEND_RETRY_DELAYS=banana,apple\n"]
)
def test_invalid_file_values_fail_without_mutating_runtime(tmp_path: Path, line: str) -> None:
    path = tmp_path / ".env"
    authority = _authority(path)
    before = authority.flat_dump()
    path.write_text(line)

    with pytest.raises(ValidationError):
        authority.current_field_values()
    assert authority.flat_dump() == before


def test_domain_validation_keeps_cross_field_invariants(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    authority = _authority(path)
    path.write_text("AVA_EXEC_TIMEOUT_SECONDS=1200\nAVA_EXEC_NODE_TIMEOUT_SECONDS=1200\n")

    with pytest.raises(ValidationError):
        authority.current_field_values()


def test_valid_file_values_do_not_read_later_ambient_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    authority = _authority(path)
    path.write_text("AVA_IM_DISABLED_ADAPTERS=weixin,feishu\n")
    monkeypatch.setitem(os.environ, "AVA_IM_SEND_RETRY_DELAYS", "banana")

    assert authority.current_field_values()["im_disabled_adapters"] == ["weixin", "feishu"]


def test_agent_owner_file_uses_explicit_runner_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".ava"
    home.mkdir()
    path = home / ".env"
    complete = _complete(db_user="ava_g0_runner")
    complete.data_plane.cluster_secret = secrets.token_urlsafe(16)
    runtime = complete.model_copy(update={"profile": "agent"})
    authority = ConfigAuthority(runtime=runtime, all_domains=complete, env_path=path)
    path.write_text("AVA_DB_URL=postgresql://ava@127.0.0.1:5433/ava\n")
    monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "agent")
    monkeypatch.setenv("AVA_HOME", str(home))

    assert authority.current_field_values()["db_url"] == runtime.data_plane.db_url
    path.write_text("AVA_DB_URL=postgresql://[::1\n")
    with pytest.raises(ValidationError):
        authority.current_field_values()


def test_agent_projection_does_not_hide_another_invalid_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".ava"
    home.mkdir()
    path = home / ".env"
    complete = _complete(db_user="ava_g0_runner")
    complete.data_plane.cluster_secret = secrets.token_urlsafe(16)
    runtime = complete.model_copy(update={"profile": "agent"})
    authority = ConfigAuthority(runtime=runtime, all_domains=complete, env_path=path)
    path.write_text("AVA_DB_URL=postgresql://ava@127.0.0.1:5433/ava\nAVA_DB_POOL_MAX_SIZE=banana\n")
    monkeypatch.setitem(os.environ, "AVA_PROCESS_PROFILE", "agent")
    monkeypatch.setenv("AVA_HOME", str(home))

    with pytest.raises(ValidationError):
        authority.current_field_values()


def test_bootstrap_distributes_declared_keys_and_credential_free_endpoint(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    password = secrets.token_urlsafe(16)
    path.write_text(
        f"AVA_DB_URL=postgresql://ava:{password}@127.0.0.1:5433/ava\n"
        "DECLARED_API_KEY=provider-secret\nUNDECLARED_API_KEY=unrelated-secret\n"
        "AVA_TELEMETRY_OTLP_PORT=4381\n"
    )
    complete = _complete(machine_host="10.0.0.3")
    authority = ConfigAuthority(runtime=complete, all_domains=complete, env_path=path)

    packet = authority.bootstrap_config_values(
        provider_key_envs=("DECLARED_API_KEY",), plugin_cluster_config="{}"
    )
    assert packet["DECLARED_API_KEY"] == "provider-secret"
    assert "UNDECLARED_API_KEY" not in packet
    assert packet["AVA_DB_URL"] == "postgresql://ava@10.0.0.3:5433/ava"
    assert packet["AVA_GATEWAY_OTLP_ENDPOINT"] == "http://10.0.0.3:4381"
    assert password not in "".join(packet.values())


def test_bootstrap_preserves_raw_values_for_recipient_validation(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("AVA_DB_URL=postgresql://ava@127.0.0.1:5433/ava\nAVA_TRACE_ENABLED=banana\n")
    authority = _authority(path)

    packet = authority.bootstrap_config_values(provider_key_envs=(), plugin_cluster_config="{}")
    assert packet["AVA_TRACE_ENABLED"] == "banana"
    with pytest.raises(ValidationError):
        authority.current_field_values()


def test_domain_batch_validates_a_joint_file_change(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    authority = _authority(path)
    path.write_text("AVA_EXEC_TIMEOUT_SECONDS=1800\nAVA_EXEC_NODE_TIMEOUT_SECONDS=2000\n")

    values = authority.current_field_values()
    assert values["exec_timeout_seconds"] == 1800
    assert values["exec_node_timeout_seconds"] == 2000


def test_repair_cli_can_fix_invalid_file_without_authority(tmp_path: Path) -> None:
    import subprocess
    import sys

    from dotenv import dotenv_values

    path = tmp_path / ".env"
    path.write_text(
        "AVA_DB_URL=postgresql://ava@127.0.0.1:5433/ava\n"
        "AVA_REDIS_URL=redis://127.0.0.1:6379/0\n"
        "AVA_EXEC_TIMEOUT_SECONDS=1800\nAVA_EXEC_NODE_TIMEOUT_SECONDS=1200\n"
        "AVA_OPS_CONCURRENCY=7\nAVA_MACHINE_SERVE_GATEWAY=true\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli.main",
            "config",
            "set",
            "AVA_EXEC_NODE_TIMEOUT_SECONDS=2000",
            "--local",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env={"AVA_CONFIG_FETCH": "skip", "AVA_HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert dotenv_values(path)["AVA_EXEC_NODE_TIMEOUT_SECONDS"] == "2000.0"
    assert dotenv_values(path)["AVA_OPS_CONCURRENCY"] == "7"


def test_deferred_complete_model_builds_once_and_keeps_file_reads_fresh(tmp_path: Path) -> None:
    complete = _complete()
    runtime = complete.model_copy(update={"profile": "gateway"})
    builds: list[Settings] = []

    def build() -> Settings:
        builds.append(complete)
        return complete

    path = tmp_path / ".env"
    authority = ConfigAuthority.deferred(runtime=runtime, build_all_domains=build, env_path=path)
    assert builds == []
    assert authority.runtime is runtime
    assert authority.service_field_value("machine_host") == runtime.general.machine_host
    assert builds == []

    def read(_index: int) -> float:
        return authority.service_field_value("exec_timeout_seconds")

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(read, range(8)))
    assert values == [complete.sandbox.exec_timeout_seconds] * 8
    assert builds == [complete]
    path.write_text("AVA_TRACE_ENABLED=false\n")
    assert authority.current_field_values()["trace_enabled"] is False
    path.write_text("AVA_TRACE_ENABLED=true\n")
    assert authority.current_field_values()["trace_enabled"] is True
    assert builds == [complete]


@pytest.mark.parametrize("error", [RuntimeError("factory failed"), KeyboardInterrupt("cancelled")])
def test_deferred_complete_model_propagates_failure_and_memoizes_only_success(
    tmp_path: Path, error: BaseException
) -> None:
    builds: list[None] = []

    complete = _complete()

    def build() -> Settings:
        builds.append(None)
        if len(builds) == 1:
            raise error
        return complete

    runtime = complete.model_copy(update={"profile": "gateway"})
    authority = ConfigAuthority.deferred(
        runtime=runtime, build_all_domains=build, env_path=tmp_path / ".env"
    )
    assert builds == []
    with pytest.raises(type(error)) as raised:
        authority.service_field_value("exec_timeout_seconds")
    assert raised.value is error
    assert builds == [None]
    assert (
        authority.service_field_value("exec_timeout_seconds")
        == complete.sandbox.exec_timeout_seconds
    )
    assert (
        authority.service_field_value("exec_timeout_seconds")
        == complete.sandbox.exec_timeout_seconds
    )
    assert builds == [None, None]


def test_deferred_complete_model_validates_profile_on_first_read(tmp_path: Path) -> None:
    builds: list[None] = []

    def build() -> Settings:
        builds.append(None)
        return _complete().model_copy(update={"profile": "agent"})

    runtime = _complete().model_copy(update={"profile": "gateway"})
    authority = ConfigAuthority.deferred(
        runtime=runtime, build_all_domains=build, env_path=tmp_path / ".env"
    )
    assert builds == []
    failures: list[ValueError] = []
    for _ in range(2):
        with pytest.raises(ValueError, match="profile-independent") as raised:
            authority.service_field_value("exec_timeout_seconds")
        failures.append(raised.value)
    assert failures[0] is not failures[1]
    assert builds == [None, None]
