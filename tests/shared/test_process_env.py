from __future__ import annotations

import pytest

from shared.process_env import forwarded_proxy_env

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
