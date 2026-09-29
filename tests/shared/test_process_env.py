from __future__ import annotations

import pytest

from shared.process_env import daemon_process_env, forwarded_proxy_env

_PROXY_NAMES = (
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
)


def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_forwarded_proxy_env_copies_only_proxy_names_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:1080/")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,.internal")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/run/secrets/viewer.json")
    monkeypatch.setenv("PGPASSWORD", "viewer-only")

    assert forwarded_proxy_env() == {
        "http_proxy": "http://127.0.0.1:7890",
        "HTTPS_PROXY": "socks5://127.0.0.1:1080/",
        "NO_PROXY": "localhost,127.0.0.1,.internal",
    }


def test_forwarded_proxy_env_drops_empty_and_absent_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_env(monkeypatch)
    assert forwarded_proxy_env() == {}

    for name in _PROXY_NAMES:
        monkeypatch.setenv(name, "")
    assert forwarded_proxy_env() == {}


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
