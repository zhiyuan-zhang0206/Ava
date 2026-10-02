"""A bearer-only runner receives no database login, and a revoked generation's bundle never installs (real PostgreSQL, PgBouncer and Redis)."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest

from base import config
from base.cluster import authority
from base.cluster.authority import fence, unit
from base.cluster.authority.tests.unit_capability_support import _MACHINE, _issue, _open
from base.config.service_read import served_db_endpoint
from cli.commands.data_plane import bringup
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.tests import test_single_box as _single_box
from cli.commands.tests.test_single_box import Born, _refused

configured = _single_box.configured
born = _single_box.born


def _refusing_probe(_dsn: str) -> None:
    raise AssertionError("an older bundle is refused before its login is probed")


def _serve_on_loopback(monkeypatch: pytest.MonkeyPatch, born: Born) -> None:
    """Serve bootstrap from `born`'s `.env`. The single-box gateway binds
    loopback (empty bearer), so its served endpoint must stay on loopback for
    the co-located runner under test."""
    from base.host.env import runtime_config

    monkeypatch.setattr(runtime_config, "_ava_home", lambda: born.home)
    monkeypatch.setattr(config, "_self_machine_host", lambda: "localhost")
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "true")


def test_a_bearer_only_runner_receives_no_database_login(
    born: Born, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retired exchange: a stale runner holding the bearer fetches
    bootstrap. The payload carries no generation login, and its endpoint alone
    opens no door."""
    _serve_on_loopback(monkeypatch, born)
    served = config.bootstrap_config_values()
    payload = "".join(served.values())
    for cls in ("gateway", "runner"):
        name, password = born.login(cls)
        assert name not in payload and password not in payload
    endpoint = served["AVA_DB_URL"]
    assert urlsplit(endpoint).password is None
    _refused(conninfo=endpoint)


def _rotate(born: Born) -> None:
    """One release rotation: fence the active generation, mint the next, and
    restart the pooler serving only the new pair."""
    operation = authority.OperationAuthority(operation=uuid4(), direction="candidate")
    pooler.stop_pgbouncer(force=True)
    with bringup.admin_session(born.record, "ava") as conn:
        fence.revoke(conn, born.home, operation)
        fence.close_revoked(conn, born.home, operation)
        verified = authority.mint_generation(conn, born.home, operation)
    authority.activate(born.home, operation, verified)
    bringup._ensure_pooler(born.record, "ava", born.home, authority.active_generation(born.home))


def test_a_revoked_generations_bundle_never_installs(
    born: Born, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_on_loopback(monkeypatch, born)
    endpoint = served_db_endpoint()
    runner, spare = (tmp_path / "runner").resolve(), (tmp_path / "spare").resolve()
    for home in (runner, spare):
        home.mkdir(mode=0o700)
    g0_runner = _issue(born.home, runner, endpoint=endpoint)
    g0_spare = _issue(born.home, spare, endpoint=endpoint)
    installed = unit.install_bundle(
        runner, _open(g0_runner), machine=_MACHINE, served_endpoint=endpoint
    )
    assert installed.generation.number == 0

    _rotate(born)

    # The installed generation-0 login is refused by the cluster, and a
    # generation-0 bundle never installs: the database is the arbiter.
    _refused(conninfo=installed.dsn)
    with pytest.raises(unit.UnitCapabilityError, match="refuses generation 0"):
        unit.install_bundle(spare, _open(g0_spare), machine=_MACHINE, served_endpoint=endpoint)
    assert unit.load_unit_capability(spare) is None
    # The current generation supersedes; an older bundle is then refused
    # before its login is ever tried.
    current = unit.install_bundle(
        runner,
        _open(_issue(born.home, runner, endpoint=endpoint)),
        machine=_MACHINE,
        served_endpoint=endpoint,
    )
    assert current.generation.number == 1
    with psycopg.connect(current.dsn, prepare_threshold=None, connect_timeout=5) as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ava_g1_runner",)
    with pytest.raises(unit.UnitCapabilityError, match="older than the installed generation 1"):
        unit.install_bundle(
            runner,
            _open(g0_runner),
            machine=_MACHINE,
            served_endpoint=endpoint,
            probe=_refusing_probe,
        )
