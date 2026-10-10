"""A bearer-only runner receives no database login, and a bundle whose login cannot be proven never installs (real PostgreSQL, PgBouncer and Redis)."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

from base import config
from base.cluster import authority
from base.cluster.authority import unit
from base.cluster.authority.tests.unit_capability_support import _MACHINE, _issue, _open
from base.config.service_read import ConfigAuthority, served_db_endpoint
from base.lm.plugin_providers import build_model_catalog
from base.packages.plugins.config_face import plugin_bootstrap_config
from cli.commands.data_plane import bringup
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.tests import test_single_box as _single_box
from cli.commands.tests.test_single_box import Born, _refused

configured = _single_box.configured
born = _single_box.born


def _serve_on_loopback(monkeypatch: pytest.MonkeyPatch, born: Born) -> ConfigAuthority:
    """Serve bootstrap from `born`'s `.env`. The single-box gateway binds
    loopback (empty bearer), so its served endpoint must stay on loopback for
    the co-located runner under test."""
    from base.host.env import runtime_config

    monkeypatch.setattr(runtime_config, "_ava_home", lambda: born.home)
    monkeypatch.setattr(config.settings.general, "machine_host", "localhost")
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "true")
    complete = config.settings if config.settings.profile is None else config.Settings(profile=None)
    return ConfigAuthority(config.settings, complete, born.home / ".env")


def test_a_bearer_only_runner_receives_no_database_login(
    born: Born, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retired exchange: a stale runner holding the bearer fetches
    bootstrap. The payload carries no generation login, and its endpoint alone
    opens no door."""
    bootstrap_authority = _serve_on_loopback(monkeypatch, born)
    served = config.bootstrap_config_values(
        bootstrap_authority,
        provider_key_envs=(binding.key_env for binding in build_model_catalog().bindings.values()),
        plugin_cluster_config=plugin_bootstrap_config(),
    )
    payload = "".join(served.values())
    for cls in ("gateway", "runner"):
        name, password = born.login(cls)
        assert name not in payload and password not in payload
    endpoint = served["AVA_DB_URL"]
    assert urlsplit(endpoint).password is None
    _refused(conninfo=endpoint)


def test_a_bundle_whose_login_cannot_be_proven_never_installs(
    born: Born, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap_authority = _serve_on_loopback(monkeypatch, born)
    endpoint = served_db_endpoint(bootstrap_authority)
    runner = (tmp_path / "runner").resolve()
    runner.mkdir(mode=0o700)
    bundle = _issue(born.home, runner, endpoint=endpoint)
    pooler.stop_pgbouncer(force=True)
    # The served endpoint is the arbiter: a login it does not answer never installs.
    with pytest.raises(unit.UnitCapabilityError, match="refuses generation 0"):
        unit.install_bundle(runner, _open(bundle), machine=_MACHINE, served_endpoint=endpoint)
    assert unit.load_unit_capability(runner) is None
    bringup._ensure_pooler(born.record, "ava", born.home, authority.active_generation(born.home))
    installed = unit.install_bundle(
        runner, _open(bundle), machine=_MACHINE, served_endpoint=endpoint
    )
    assert installed.generation.number == 0
    with psycopg.connect(installed.dsn, prepare_threshold=None, connect_timeout=5) as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ava_g0_runner",)
    # A re-issued bundle of the same generation installs again.
    again = unit.install_bundle(
        runner,
        _open(_issue(born.home, runner, endpoint=endpoint)),
        machine=_MACHINE,
        served_endpoint=endpoint,
    )
    assert again.generation == installed.generation
