from __future__ import annotations

import pytest

from base.native_process.child_env import daemon_process_env


def test_daemon_env_keeps_the_process_essentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pg_start_env` builds on this set. These are process mechanics, not credentials."""
    essentials = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/operator",
        "TMPDIR": "/home/operator/tmp",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
    }
    for name, value in essentials.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AVA_API_TOKEN", "gateway-api-token-" + "t" * 32)

    env = daemon_process_env()

    assert {name: env[name] for name in essentials} == essentials
    assert "AVA_API_TOKEN" not in env
