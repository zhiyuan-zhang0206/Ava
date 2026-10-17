"""A home's Redis tools stay selected across a boot whose inherited env is polluted."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from base.config.service_read import ConfigAuthority
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config


def _tools(path: Path, version: str) -> Path:
    path.mkdir(parents=True)
    for name in ("redis-server", "redis-cli"):
        tool = path / name
        tool.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
        tool.chmod(0o700)
    return path


def _home(path: Path, selected: Path | None) -> Path:
    path.mkdir()
    content = (
        "MEIVA_DB_URL=postgresql://unused@127.0.0.1:1/unused\nMEIVA_REDIS_URL=redis://127.0.0.1:1\n"
    )
    if selected is not None:
        content += f"MEIVA_REDIS_BIN_DIR='{selected}'\n"
    (path / ".env").write_text(content)
    return path


def _probe(home: Path, system_bin: Path, *, inherited: str = "") -> dict[str, Any]:
    # Explicitly isolated config; no Meiva imports in this child can dial production.
    env = {
        "HOME": str(home.parent),
        "PATH": str(system_bin),
        "MEIVA_HOME": str(home),
        "MEIVA_CONFIG_FETCH": "skip",
        "MEIVA_PROCESS_PROFILE": "gateway",
        "MEIVA_REDIS_BIN_DIR": inherited,
    }
    result = subprocess.run(  # noqa: S603 — fixed interpreter and test-owned source/config.
        [sys.executable, str(Path(__file__).with_name("child_redis_probe.py"))],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_home_selection_survives_a_polluted_boot_env(tmp_path: Path) -> None:
    selected = _tools(tmp_path / "Redis 8" / "bin", "selected-8")
    system_bin = _tools(tmp_path / "system-bin", "system-7")
    target = _home(tmp_path / "gateway", selected)
    sibling = _home(tmp_path / "preview", None)
    sibling_bytes = (sibling / ".env").read_bytes()

    actual = _probe(target, system_bin, inherited="/wrong/parent/bin")
    assert actual["configured"] == str(selected)
    assert actual["tools"] == [str(selected / name) for name in ("redis-server", "redis-cli")]
    assert actual["versions"] == ["selected-8", "selected-8"]

    other = _probe(sibling, system_bin, inherited=str(selected))
    assert other["configured"] == ""
    assert other["tools"] == ["redis-server", "redis-cli"]
    assert other["versions"] == ["system-7", "system-7"]
    assert (sibling / ".env").read_bytes() == sibling_bytes


@pytest.mark.parametrize("missing", ["redis-server", "redis-cli"])
def test_explicit_directory_requires_both_tools(
    tmp_path: Path, missing: str, data_plane_config: DataPlaneConfig
) -> None:
    selected = _tools(tmp_path / "bin", "selected-8")
    (selected / missing).unlink()
    data_plane_config = dataclasses.replace(data_plane_config, redis_bin_dir=str(selected))
    for resolver in (instance.redis_server_bin, instance.redis_cli_bin):
        with pytest.raises(RuntimeError, match=missing):
            resolver(data_plane_config)


def test_explicit_nonexecutable_tool_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data_plane_config: DataPlaneConfig
) -> None:
    selected = _tools(tmp_path / "bin", "selected-8")
    (selected / "redis-cli").chmod(0o600)
    data_plane_config = dataclasses.replace(data_plane_config, redis_bin_dir=str(selected))
    with pytest.raises(RuntimeError, match="redis-cli"):
        instance.redis_server_bin(data_plane_config)


@pytest.mark.parametrize("value", ["relative/bin", "~/redis/bin", "/redis\n/bin"])
def test_directory_setting_rejects_ambient_path_interpretation(value: str) -> None:
    from base.config.domains.storage.data_plane import DataPlaneSettings

    with pytest.raises(ValidationError, match="absolute directory"):
        DataPlaneSettings(
            MEIVA_DB_URL="postgresql://unused@127.0.0.1:1/unused",
            MEIVA_REDIS_URL="redis://127.0.0.1:1",
            MEIVA_REDIS_BIN_DIR=value,
        )


def test_config_is_local_writable_and_not_a_runner_bootstrap_fact(
    config_authority: ConfigAuthority,
) -> None:
    from base.config import BOOTSTRAP_FIELDS, get_config_metadata

    field = next(
        item
        for item in get_config_metadata(authority=config_authority)
        if item.name == "redis_bin_dir"
    )
    assert field.scope == "host"
    assert field.writable and not field.remote_writable
    assert field.env_var == "MEIVA_REDIS_BIN_DIR"
    assert "redis_bin_dir" not in BOOTSTRAP_FIELDS


def test_local_config_write_only_changes_target_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from base.host.env import dotenv_file
    from cli.commands.management import config as config_cli

    selected = _tools(tmp_path / "bin", "selected-8")
    target = _home(tmp_path / "gateway", None)
    sibling = _home(tmp_path / "preview", None)
    sibling_bytes = (sibling / ".env").read_bytes()
    monkeypatch.setattr(dotenv_file, "unit_home", lambda: target)
    target_bytes = (target / ".env").read_bytes()
    assert config_cli.cmd_config_set(["redis_bin_dir=relative/bin"], None, local=True) == 1
    assert (target / ".env").read_bytes() == target_bytes
    assert config_cli.cmd_config_set([f"redis_bin_dir={selected}"], None, local=True) == 0
    assert dotenv_file.read_env_aliases()["MEIVA_REDIS_BIN_DIR"] == str(selected)
    capsys.readouterr()
    assert config_cli.cmd_config_get("redis_bin_dir", None, local=True) == 0
    assert str(selected) in capsys.readouterr().out
    assert config_cli.cmd_config_unset(["redis_bin_dir"], None, local=True) == 0
    assert "MEIVA_REDIS_BIN_DIR" not in dotenv_file.read_env_aliases()
    assert (sibling / ".env").read_bytes() == sibling_bytes
