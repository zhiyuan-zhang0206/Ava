from __future__ import annotations

import pytest

from base.native_process.child_env import daemon_process_env


def test_daemon_env_keeps_the_windows_process_essentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pg_start_env` builds on this set, and a throwaway Postgres also starts on
    Windows, where a child needs `SystemRoot` to run at all and `pg_ctl` starts
    the server through `COMSPEC`. These are process mechanics, not credentials."""
    essentials = {
        "SYSTEMROOT": "C:\\Windows",
        "WINDIR": "C:\\Windows",
        "SYSTEMDRIVE": "C:",
        "COMSPEC": "C:\\Windows\\system32\\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": "C:\\Temp",
        "TMP": "C:\\Temp",
        "USERPROFILE": "C:\\Users\\operator",
    }
    for name, value in essentials.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AVA_API_TOKEN", "gateway-api-token-" + "t" * 32)

    env = daemon_process_env()

    assert {name: env[name] for name in essentials} == essentials
    assert "AVA_API_TOKEN" not in env
