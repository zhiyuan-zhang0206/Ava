"""`ava cluster db-authority install-unit` installs a sealed capability on an initialized runner."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from base import cluster
from base.cluster.authority import unit
from base.host.env import bootstrap
from cli import start_identity
from cli.commands.cluster.control import cmd_db_authority_install_unit

_ENDPOINT = "postgresql://ava@remote.invalid/db"
_MACHINE = "mini"
_GATEWAY_URL = "http://127.0.0.1:1"


def _every_port_free(_port: int) -> bool:
    return True


@pytest.fixture(autouse=True)
def _isolate_environment() -> Iterator[None]:
    with patch.dict(os.environ):
        os.environ.pop(unit.CAPABILITY_KEY_ENV, None)
        yield


@pytest.fixture
def bearers(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The gateway answers the bootstrap fetch; each bearer it was shown is recorded."""
    seen: list[object] = []

    def served(*_a: object, **kwargs: object) -> dict[str, str]:
        seen.append(kwargs.get("bearer"))
        return {"AVA_DB_URL": _ENDPOINT, "AVA_REDIS_URL": "redis://remote.invalid/0"}

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", served)
    # The seeded ledger's logins are no PostgreSQL roles: skip the install's login probe.
    monkeypatch.setattr(
        unit, "install_bundle", partial(unit.install_bundle, probe=lambda _dsn: None)
    )
    return seen


@pytest.fixture
def issue(tmp_path: Path, seed_write_generation: Callable[[Path], Any]) -> Callable[..., Path]:
    """Seal a bundle on a gateway home for a unit; the transport key is exported for it."""
    gateway = tmp_path / "gateway"
    gateway.mkdir(mode=0o700)
    seed_write_generation(gateway)

    def make(name: str, *, machine: str = _MACHINE, home: Path | None = None) -> Path:
        issued = unit.issue_bundle(
            gateway.resolve(),
            unit=unit.UnitIdentity(
                machine=machine, home=str((home or tmp_path / "runner").resolve())
            ),
            endpoint=_ENDPOINT,
            cluster_secret="gateway-human-secret-" + "g" * 32,
            ttl_s=60,
        )
        path = tmp_path / name
        path.write_bytes(issued.envelope)
        os.environ[unit.CAPABILITY_KEY_ENV] = issued.transport_key
        return path

    return make


def _runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, roles: frozenset[str] | None = None
) -> Path:
    """An initialized home: what `ava init` leaves."""
    home = (tmp_path / "runner").resolve()
    roles = roles or frozenset({"agent-runner"})
    values = {
        "AVA_MACHINE_NAME": _MACHINE,
        "AVA_GATEWAY_URL": _GATEWAY_URL,
        "AVA_SERVICE_PATH": str(tmp_path / "tools"),
    }
    monkeypatch.setattr(cluster, "port_free", _every_port_free)
    start_identity.prepare_identity(
        start_identity.IdentityInput(home, tmp_path / "checkout", roles, values)
    )
    monkeypatch.setenv("AVA_HOME", str(home))
    return home


def test_it_installs_the_bundle_deletes_it_and_presents_the_bundles_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issue: Callable[..., Path],
    bearers: list[object],
) -> None:
    home = _runner(tmp_path, monkeypatch)
    bundle = issue("unit.bundle")

    assert cmd_db_authority_install_unit(bundle=str(bundle)) == 0

    installed = unit.require_unit_capability(home)
    assert installed.api is not None and bearers == [installed.api.token]
    assert not bundle.exists() and unit.CAPABILITY_KEY_ENV not in os.environ


def test_a_fresh_bundle_of_the_same_generation_renews_the_installed_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issue: Callable[..., Path],
    bearers: list[object],
) -> None:
    home = _runner(tmp_path, monkeypatch)
    assert cmd_db_authority_install_unit(bundle=str(issue("first.bundle"))) == 0
    first = unit.require_unit_capability(home)

    renewal = issue("renewal.bundle")
    assert cmd_db_authority_install_unit(bundle=str(renewal)) == 0

    assert unit.require_unit_capability(home).generation == first.generation
    assert not renewal.exists() and len(bearers) == 2


def test_it_refuses_a_gateway_home_and_keeps_the_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issue: Callable[..., Path],
    bearers: list[object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _runner(tmp_path, monkeypatch, roles=frozenset({"gateway", "agent-runner"}))
    bundle = issue("unit.bundle")

    assert cmd_db_authority_install_unit(bundle=str(bundle)) == 1

    assert "keeps its own write-generation ledger" in capsys.readouterr().err
    assert bundle.exists() and bearers == []
    assert unit.load_unit_capability(home) is None


def test_it_refuses_a_home_init_has_not_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issue: Callable[..., Path],
    bearers: list[object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AVA_HOME", str((tmp_path / "runner").resolve()))
    bundle = issue("unit.bundle")

    assert cmd_db_authority_install_unit(bundle=str(bundle)) == 1

    assert "not initialized: run `ava init` first" in capsys.readouterr().err
    assert bundle.exists() and bearers == []


def test_it_needs_the_transport_key_and_keeps_the_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issue: Callable[..., Path],
    bearers: list[object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _runner(tmp_path, monkeypatch)
    bundle = issue("unit.bundle")
    del os.environ[unit.CAPABILITY_KEY_ENV]

    assert cmd_db_authority_install_unit(bundle=str(bundle)) == 1

    assert unit.CAPABILITY_KEY_ENV in capsys.readouterr().err
    assert bundle.exists() and bearers == [] and unit.load_unit_capability(home) is None


def test_it_refuses_another_units_bundle_and_keeps_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issue: Callable[..., Path],
    bearers: list[object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _runner(tmp_path, monkeypatch)
    bundle = issue("other.bundle", machine="elsewhere")

    assert cmd_db_authority_install_unit(bundle=str(bundle)) == 1

    assert "was issued for" in capsys.readouterr().err
    assert bundle.exists() and unit.load_unit_capability(home) is None
