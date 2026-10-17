"""Shared sentinel environment and real PTY argv inspection for launcher contracts."""

from __future__ import annotations

import os

import psutil
import pytest

from base.sessions.pty.tests.pty_service import PtyServiceProcess

# Values that must never appear in an argv. Shaped like the real thing: the
# cluster secret, the data-plane URLs that embed it, a provider key.
SECRET = "sentinel-cluster-secret-6f21ab"  # noqa: S105 — a sentinel to search argv for, not a credential
DB_URL = f"postgresql://meiva:{SECRET}@10.0.0.4:5433/meiva"
REDIS_URL = f"redis://meiva:{SECRET}@10.0.0.4:6380/0"
API_KEY = "sk-sentinel-provider-key-4c19"
SECRET_VALUES = (SECRET, DB_URL, REDIS_URL, API_KEY)

SECRET_ENV = {
    "MEIVA_HOME": "/tmp/meiva-home",  # noqa: S108 — a literal env value, never opened
    "MEIVA_CLUSTER_SECRET": SECRET,
    "MEIVA_DB_URL": DB_URL,
    "MEIVA_REDIS_URL": REDIS_URL,
    "DEEPSEEK_API_KEY": API_KEY,
    "PATH": "/usr/bin:/bin",
}


def assert_clean(argv: list[str], *, label: str) -> None:
    for element in argv:
        for secret in SECRET_VALUES:
            assert secret not in element, f"{label} leaked a secret on argv: {argv!r}"


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live env a daemon launcher forwards. Replaced wholesale — the
    forwarders read `os.environ` by nature (see no_os_environ's allowlist)."""
    monkeypatch.setattr(os, "environ", dict(SECRET_ENV))


@pytest.fixture
def secrets_in_creator_env(pty_service: PtyServiceProcess, monkeypatch: pytest.MonkeyPatch) -> None:
    """The creating process holds the secrets (as an agent or gateway does); the
    already-running service does not, so a secret reaching a shell could only have
    come through the request."""
    del pty_service
    for key, value in SECRET_ENV.items():
        if key not in ("MEIVA_HOME", "PATH"):
            monkeypatch.setenv(key, value)


def service_tree_argvs(service: PtyServiceProcess) -> list[list[str]]:
    """The argv of the service process and of every process under it (shells, jobs)."""
    root = psutil.Process(service.pid)
    argvs = [root.cmdline()]
    for child in root.children(recursive=True):
        try:
            argvs.append(child.cmdline())
        except psutil.NoSuchProcess:
            continue
    return argvs


def assert_service_tree_clean(service: PtyServiceProcess, *, label: str) -> None:
    argvs = service_tree_argvs(service)
    assert len(argvs) >= 2, f"{label}: expected the service and a shell, saw {argvs!r}"
    for argv in argvs:
        assert_clean(argv, label=label)
