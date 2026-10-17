from __future__ import annotations

import pytest

from base.native_process.child_env import daemon_process_env


def test_daemon_env_keeps_the_process_essentials(monkeypatch: pytest.MonkeyPatch) -> None:
    essentials = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/operator",
        "TMPDIR": "/home/operator/tmp",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
    }
    for name, value in essentials.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MEIVA_API_TOKEN", "gateway-api-token-" + "t" * 32)

    env = daemon_process_env()

    assert {name: env[name] for name in essentials} == essentials
    assert "MEIVA_API_TOKEN" not in env
