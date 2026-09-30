"""Tests for base.sessions.env_forwarding: session env forwarding (daemon/service children).

The env rides a built dict handed to the child process out-of-band — never an
argv (issue #974: the old handoff published the cluster secret and
every provider key to `ps`), and no 0600 handoff file since the native
supervisors take the dict directly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from base.sessions import env_forwarding


@pytest.mark.parametrize("runtime", ["runtime", "project #1/runtime"])
def test_managed_service_path_preserves_tools_across_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    from base.deploy.release import runtime_interpreter

    venv = tmp_path / runtime
    bindir = venv / env_forwarding.get_backend().venv_bin_dir_name()
    tools = tmp_path / "admitted-tools"
    tools.mkdir()
    command = tools / "ava-proof"
    command.write_bytes(b"proof")
    command.chmod(0o700)
    monkeypatch.setattr(runtime_interpreter, "runtime_venv", lambda: venv)
    admitted = os.pathsep.join((str(tools), str(bindir), str(tools)))
    monkeypatch.setenv("PATH", str(tmp_path / "interactive"))
    interactive = env_forwarding.managed_service_env(admitted)
    monkeypatch.setenv("PATH", str(tmp_path / "manager"))
    manager = env_forwarding.managed_service_env(admitted)
    assert interactive == manager
    assert manager["PATH"].split(os.pathsep)[:2] == [str(bindir), str(tools)]
    assert manager["PATH"].split(os.pathsep).count(str(bindir)) == 1
    assert manager["AVA_SERVICE_PATH"] == str(tools)
    assert shutil.which(command.name, path=manager["PATH"]) == str(command)
    assert env_forwarding.managed_service_env(str(tmp_path / "changed")) != manager


@pytest.mark.parametrize("entry", ["relative", ".", "bad\npath", "bad\x00path"])
def test_managed_service_path_refuses_caller_relative_entries(entry: str) -> None:
    with pytest.raises(ValueError, match="absolute directory"):
        env_forwarding.normalize_service_path(entry)


@pytest.mark.parametrize("name", ["tools #1", "${CALLER_TOOLS}", "trailing "])
def test_managed_service_path_refuses_lossy_home_declarations(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="round-trip literally"):
        env_forwarding.admit_service_path(str(tmp_path / name))


def test_managed_service_path_accepts_plain_spaces(tmp_path: Path) -> None:
    path = str(tmp_path / "tools with spaces")
    assert env_forwarding.admit_service_path(path) == path


@pytest.mark.parametrize("shell_path", ["/usr/bin:/bin", "/bin"])
def test_frontend_toolchain_path_is_independent_of_shell_profile(shell_path: str) -> None:
    """The Node locations are injected even when a remote shell exports only system dirs."""
    assert env_forwarding.frontend_toolchain_path(shell_path).split(":")[:4] == [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


def test_venv_activation_prefix_omits_an_empty_path_entry() -> None:
    """An empty login-shell PATH does not add the working directory to lookup."""
    assert "${PATH:+:$PATH}" in env_forwarding.venv_activation_prefix()


def test_forward_env_dict_drops_cluster_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """`forward_env_dict` (the `ava start` daemon child env)
    drops the cluster-scope keys too — same policy as the env-file prefix it
    replaced, and it keeps the whole-env copy's PATH the child genuinely needs."""
    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_HOME": "/tmp/ava-home",  # noqa: S108 — a literal env value, never opened
            "AVA_GATEWAY_URL": "http://gw:9000",
            "AVA_DB_URL": "postgresql://x/db",
            "AVA_REDIS_URL": "redis://x/0",
            "DEEPSEEK_API_KEY": "sk-x",
            "AVA_CONFIG_FETCH": "skip",
            "PATH": "/usr/bin",
        },
    )
    env = env_forwarding.forward_env_dict()
    assert env["AVA_HOME"] == "/tmp/ava-home"  # noqa: S108 — literal, never opened
    assert env["AVA_GATEWAY_URL"] == "http://gw:9000"
    assert "/usr/bin" in env["PATH"]  # whole-env copy keeps PATH (venv-prefixed)
    assert "AVA_DB_URL" not in env
    assert "AVA_REDIS_URL" not in env
    assert "DEEPSEEK_API_KEY" not in env
    assert "AVA_CONFIG_FETCH" not in env
    assert "AVA_CONFIG_SOURCE" not in env


def test_forward_env_dict_without_venv_activation_keeps_path_but_omits_virtual_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign-cwd shell still finds tools on PATH without selecting this venv for uv."""

    monkeypatch.setattr(
        os,
        "environ",
        {"AVA_HOME": "/tmp/ava-home", "PATH": "/usr/bin"},  # noqa: S108 — literal env only
    )

    env = env_forwarding.forward_env_dict(activate_venv=False)

    assert "VIRTUAL_ENV" not in env
    assert "/usr/bin" in env["PATH"].split(os.pathsep)


def test_forward_env_dict_carries_temp_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMPDIR is copied from the parent so pinned boot files keep their path."""
    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_HOME": "/tmp/ava-home",  # noqa: S108 — a literal env value, never opened
            "PATH": "/usr/bin",
            "TMPDIR": "/tmp",  # noqa: S108 — a literal env value, never opened
        },
    )
    env = env_forwarding.forward_env_dict()
    assert env["TMPDIR"] == "/tmp"  # noqa: S108 — literal, never opened
    assert "/usr/bin" in env["PATH"].split(os.pathsep)


def test_forward_env_dict_carries_the_machine_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #2095: the service/session child env carries the machine's proxy
    configuration (`ava start` and the watchdog respawn both hand this dict to
    the session backend). The frontend's `npm run build` fetches Google Fonts for
    next/font through it, so a host whose egress is proxy-only produced a build
    that could never download a font. The value's only home is the machine's own
    environment (an exporting shell, or `.env` / `mirror.env`); an empty value is
    not forwarded (that would mean "no proxy" to a client that reads env)."""
    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_HOME": "/tmp/ava-home",  # noqa: S108 — a literal env value, never opened
            "PATH": "/usr/bin",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "no_proxy": "localhost,127.0.0.1",
            "ALL_PROXY": "",
        },
    )

    env = env_forwarding.forward_env_dict()

    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert env["no_proxy"] == "localhost,127.0.0.1"
    assert "ALL_PROXY" not in env
