"""A home's Redis tools stay selected across clean boot and polluted updater envs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cli.commands import _cluster_instance as instance
from shared.config import settings


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
        "AVA_DB_URL=postgresql://unused@127.0.0.1:1/unused\nAVA_REDIS_URL=redis://127.0.0.1:1\n"
    )
    if selected is not None:
        content += f"AVA_REDIS_BIN_DIR='{selected}'\n"
    (path / ".env").write_text(content)
    return path


def _probe(
    home: Path, system_bin: Path, *, inherited: str = "", entry: str = "boot"
) -> dict[str, Any]:
    # Explicitly isolated config; no AVA imports in this child can dial production.
    env = {
        "HOME": str(home.parent),
        "PATH": str(system_bin),
        "AVA_HOME": str(home),
        "AVA_HOME_OVERRIDE": "1",
        "AVA_CONFIG_FETCH": "skip",
        "AVA_PROCESS_PROFILE": "gateway",
        "AVA_REDIS_BIN_DIR": inherited,
    }
    code = """
import json, subprocess
from cli.commands import _cluster_instance as instance
from shared.config import settings
instance.is_macos = lambda: False
tools = [instance._redis_server_bin(), instance._redis_cli_bin()]
print(json.dumps({
    'configured': settings.data_plane.redis_bin_dir,
    'tools': tools,
    'versions': [subprocess.check_output([tool, '--version'], text=True).strip() for tool in tools],
}))
"""
    if entry == "update":
        # Run the same real environment builder as the fresh updater start child.
        code = (
            "import subprocess, sys\n"
            "from shared.rollout_handoff import child_process_env\n"
            f"raise SystemExit(subprocess.call([sys.executable, '-c', {code!r}], env=child_process_env()))\n"
        )
    elif entry == "boot":
        # Real boot retry owner, replacing only `ava start` with the safe resolver
        # probe. It still launches a fresh interpreter with cron's inherited env.
        code = (
            "import subprocess, sys\n"
            "from cli import boot_retry\n"
            "run = subprocess.run\n"
            "def probe(command, **kwargs):\n"
            "    assert command[:4] == [sys.executable, '-m', 'cli.main', 'start']\n"
            f"    result = run([sys.executable, '-c', {code!r}], check=False)\n"
            "    assert result.returncode == 0\n"
            "    return result\n"
            "boot_retry.subprocess.run = probe\n"
            "raise SystemExit(boot_retry.run_boot([]))\n"
        )
    result = subprocess.run(  # noqa: S603 — fixed interpreter and test-owned source/config.
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(sys.platform == "win32", reason="Redis server is POSIX-only")
@pytest.mark.parametrize("entry", ["boot", "update"])
def test_home_selection_survives_clean_boot_and_polluted_update(tmp_path: Path, entry: str) -> None:
    selected = _tools(tmp_path / "Redis 8" / "bin", "selected-8")
    system_bin = _tools(tmp_path / "system-bin", "system-7")
    target = _home(tmp_path / "gateway", selected)
    sibling = _home(tmp_path / "preview", None)
    sibling_bytes = (sibling / ".env").read_bytes()

    actual = _probe(target, system_bin, inherited="/wrong/parent/bin", entry=entry)
    assert actual["configured"] == str(selected)
    assert actual["tools"] == [str(selected / name) for name in ("redis-server", "redis-cli")]
    assert actual["versions"] == ["selected-8", "selected-8"]

    other = _probe(sibling, system_bin, inherited=str(selected), entry=entry)
    assert other["configured"] == ""
    assert other["tools"] == ["redis-server", "redis-cli"]
    assert other["versions"] == ["system-7", "system-7"]
    assert (sibling / ".env").read_bytes() == sibling_bytes


@pytest.mark.parametrize("missing", ["redis-server", "redis-cli"])
def test_explicit_directory_requires_both_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
) -> None:
    selected = _tools(tmp_path / "bin", "selected-8")
    (selected / missing).unlink()
    monkeypatch.setattr(settings.data_plane, "redis_bin_dir", str(selected))
    for resolver in (instance._redis_server_bin, instance._redis_cli_bin):
        with pytest.raises(RuntimeError, match=missing):
            resolver()


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable permission")
def test_explicit_nonexecutable_tool_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selected = _tools(tmp_path / "bin", "selected-8")
    (selected / "redis-cli").chmod(0o600)
    monkeypatch.setattr(settings.data_plane, "redis_bin_dir", str(selected))
    with pytest.raises(RuntimeError, match="redis-cli"):
        instance._redis_server_bin()


@pytest.mark.parametrize("value", ["relative/bin", "~/redis/bin", "/redis\n/bin"])
def test_directory_setting_rejects_ambient_path_interpretation(value: str) -> None:
    from shared.config.data_plane import DataPlaneSettings

    with pytest.raises(ValidationError, match="absolute directory"):
        DataPlaneSettings(
            AVA_DB_URL="postgresql://unused@127.0.0.1:1/unused",
            AVA_REDIS_URL="redis://127.0.0.1:1",
            AVA_REDIS_BIN_DIR=value,
        )


def test_config_is_local_writable_and_not_a_runner_bootstrap_fact() -> None:
    from shared.config import BOOTSTRAP_FIELDS, get_config_metadata

    field = next(item for item in get_config_metadata() if item.name == "redis_bin_dir")
    assert field.scope == "host"
    assert field.writable and not field.remote_writable
    assert field.env_var == "AVA_REDIS_BIN_DIR"
    assert "redis_bin_dir" not in BOOTSTRAP_FIELDS


def test_local_config_write_only_changes_target_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands import config as config_cli
    from shared import runtime_config

    selected = _tools(tmp_path / "bin", "selected-8")
    target = _home(tmp_path / "gateway", None)
    sibling = _home(tmp_path / "preview", None)
    sibling_bytes = (sibling / ".env").read_bytes()
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: target)
    target_bytes = (target / ".env").read_bytes()
    assert config_cli.cmd_config_set(["redis_bin_dir=relative/bin"], None, local=True) == 1
    assert (target / ".env").read_bytes() == target_bytes
    assert config_cli.cmd_config_set([f"redis_bin_dir={selected}"], None, local=True) == 0
    assert runtime_config.read_env_aliases()["AVA_REDIS_BIN_DIR"] == str(selected)
    capsys.readouterr()
    assert config_cli.cmd_config_get("redis_bin_dir", None, local=True) == 0
    assert str(selected) in capsys.readouterr().out
    assert config_cli.cmd_config_unset(["redis_bin_dir"], None, local=True) == 0
    assert "AVA_REDIS_BIN_DIR" not in runtime_config.read_env_aliases()
    assert (sibling / ".env").read_bytes() == sibling_bytes
