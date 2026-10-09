"""The sealed unit-capability bundle's shared helpers: the gateway and runner homes, issue / open / install, and the pure runner boot."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from base.cluster import authority
from base.cluster.authority import unit
from base.cluster.authority.api import API_TOKEN_ENV
from base.host.env import dotenv_boot

_ENDPOINT = "postgresql://ava@10.0.0.7:6433/ava"
_MACHINE = "mini"
_HUMAN = "gateway-human-secret-" + "s" * 32


def _no_probe(_dsn: str) -> None:
    return None


@pytest.fixture
def gateway(tmp_path: Path, seed_write_generation: Callable[[Path], Any]) -> Path:
    home = (tmp_path / "gateway").resolve()
    home.mkdir(mode=0o700)
    seed_write_generation(home)
    return home


@pytest.fixture
def runner_home(tmp_path: Path) -> Path:
    home = (tmp_path / "runner").resolve()
    home.mkdir(mode=0o700)
    return home


def _issue(
    gateway: Path,
    home: Path,
    *,
    endpoint: str = _ENDPOINT,
    cluster_secret: str = _HUMAN,
    ttl_s: float = 600.0,
    now: float | None = None,
) -> unit.IssuedBundle:
    identity = unit.UnitIdentity(machine=_MACHINE, home=str(home))
    return unit.issue_bundle(
        gateway,
        unit=identity,
        endpoint=endpoint,
        cluster_secret=cluster_secret,
        ttl_s=ttl_s,
        now=now,
    )


def _open(issued: unit.IssuedBundle) -> unit.Bundle:
    return unit.open_bundle(issued.envelope, issued.transport_key)


def _install(home: Path, issued: unit.IssuedBundle, **kwargs: Any) -> unit.UnitCapability:
    kwargs.setdefault("served_endpoint", _ENDPOINT)
    kwargs.setdefault("probe", _no_probe)
    return unit.install_bundle(home, _open(issued), machine=_MACHINE, **kwargs)


@pytest.fixture
def runner_boot(
    monkeypatch: pytest.MonkeyPatch, runner_home: Path, gateway: Path
) -> Iterator[unit.UnitCapability]:
    """A pure agent-runner home (no database URL in `.env`) with an installed
    capability, as this process's home. The whole environment is restored
    afterwards."""
    saved = dict(os.environ)
    env_file = runner_home / ".env"
    env_file.write_text("AVA_MACHINE_SERVE_AGENT_RUNNER=true\n")
    monkeypatch.setenv("AVA_HOME", str(runner_home))
    for key in (
        "AVA_PROCESS_PROFILE",
        dotenv_boot.LAUNCHER_PROFILE_ENV_KEY,
        authority.GENERATION_ENV,
        "AVA_DB_URL",
        API_TOKEN_ENV,
    ):
        os.environ.pop(key, None)
    capability = _install(runner_home, _issue(gateway, runner_home))
    try:
        yield capability
    finally:
        os.environ.clear()
        os.environ.update(saved)
