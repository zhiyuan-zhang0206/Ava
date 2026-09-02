"""Tests for shared.session_env: session env forwarding (daemon/service children).

The env rides a built dict handed to the child process out-of-band — never an
argv (issue #974: the old handoff published the cluster secret and
every provider key to `ps`), and no 0600 handoff file since the native
supervisors take the dict directly.
"""

from __future__ import annotations

import os

import pytest

from shared import session_env
from shared.platform import IS_WINDOWS


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX toolchain paths are injected only on POSIX")
@pytest.mark.parametrize("shell_path", ["/usr/bin:/bin", "/bin"])
def test_frontend_toolchain_path_is_independent_of_shell_profile(shell_path: str) -> None:
    """The Node locations are injected even when a remote shell exports only system dirs."""
    assert session_env.frontend_toolchain_path(shell_path).split(":")[:4] == [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


def test_forward_env_dict_drops_cluster_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """`forward_env_dict` (the `ava start` daemon child env, POSIX and Windows)
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
    env = session_env.forward_env_dict()
    assert env["AVA_HOME"] == "/tmp/ava-home"  # noqa: S108 — literal, never opened
    assert env["AVA_GATEWAY_URL"] == "http://gw:9000"
    assert "/usr/bin" in env["PATH"]  # whole-env copy keeps PATH (venv-prefixed)
    assert "AVA_DB_URL" not in env
    assert "AVA_REDIS_URL" not in env
    assert "DEEPSEEK_API_KEY" not in env
    assert "AVA_CONFIG_FETCH" not in env
    assert "AVA_CONFIG_SOURCE" not in env


def test_forward_env_dict_carries_temp_dir_and_windows_system_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allowlist carries no non-Settings keys, but a child env built from
    it alone is broken in two ways fixed here: TMPDIR (a POSIX child without it
    falls back to the OS default temp root and pinned boot files drift) and the
    Windows system keys (CreateProcess replaces the child env wholesale, so a
    Windows service child without SYSTEMROOT dies in winsock init — WinError
    10106, v0.1.34 win runner). Both are copied from os.environ, non-empty
    only."""
    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_HOME": "/tmp/ava-home",  # noqa: S108 — a literal env value, never opened
            "PATH": "/usr/bin",
            "TMPDIR": "/tmp",  # noqa: S108 — a literal env value, never opened
            "SYSTEMROOT": r"C:\Windows",
            "USERNAME": "ava",
        },
    )
    # POSIX: temp dir carried (a POSIX daemon may also be spawned with this dict
    # via a direct-process backend); Windows keys not — a POSIX session runs
    # under a login shell whose profile rebuilds the full environment.
    env = session_env.forward_env_dict()
    assert env["TMPDIR"] == "/tmp"  # noqa: S108 — literal, never opened
    assert "SYSTEMROOT" not in env
    # Windows: system keys ride — the env block handed to the child is a
    # wholesale replacement, so omitting them kills the child at boot.
    monkeypatch.setattr(session_env, "IS_WINDOWS", True)
    env = session_env.forward_env_dict()
    assert env["TMPDIR"] == "/tmp"  # noqa: S108 — literal, never opened
    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["PATH"].endswith("/usr/bin")
    # USERNAME rides too: getpass.getuser() on Windows reads it and falls back
    # to `import pwd` (nonexistent there) when absent — the Task #963 updater
    # crash on every `ava start` converge at the watchdog-probe registration.
    assert env["USERNAME"] == "ava"


def test_windows_system_keys_include_username() -> None:
    """Task #963 lock: the single Windows system-keys declaration must carry
    USERNAME and USERDOMAIN. It lives in the env registry
    (shared/env_registry.py) — the old parallel copy in a legacy module is gone; a
    prune that drops USERNAME re-opens the crash: `getpass.getuser()` with no
    USERNAME in env falls through to `import pwd`, which does not exist on
    Windows (observed in every win rollout updater since the 2026-08-06
    allowlist wave, Task #963)."""
    from shared.env_registry import WINDOWS_SYSTEM_ENV_KEYS

    assert "USERNAME" in WINDOWS_SYSTEM_ENV_KEYS, "dropped USERNAME (Task #963)"
    assert "USERDOMAIN" in WINDOWS_SYSTEM_ENV_KEYS, "dropped USERDOMAIN (Task #963)"
