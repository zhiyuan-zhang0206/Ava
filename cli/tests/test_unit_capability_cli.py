"""`ava init` on a remote unit: an issued bundle starts a runner that connects as the generation login, and a home that holds the human secret refuses to join."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import psycopg
import pytest
from dotenv import dotenv_values

from base import config
from base.cluster import authority
from base.cluster.authority import unit
from base.cluster.authority.tests.unit_capability_support import _HUMAN, _MACHINE
from base.config import settings
from base.host.env import bootstrap
from cli import start_intent, unit_join
from cli.commands.data_plane import bringup
from cli.commands.tests import test_single_box as _single_box
from cli.commands.tests.test_single_box import Born
from cli.tests.bootstrap._init_identity import prepare_init_identity

configured = _single_box.configured
born = _single_box.born


def _serve_on_loopback(monkeypatch: pytest.MonkeyPatch, born: Born) -> None:
    """Serve bootstrap from `born`'s `.env`. The single-box gateway binds
    loopback (empty bearer), so its served endpoint must stay on loopback for
    the co-located runner under test."""
    from base.host.env import runtime_config

    monkeypatch.setattr(runtime_config, "_ava_home", lambda: born.home)
    monkeypatch.setattr(
        "base.config.domains.storage.data_plane.self_machine_host", lambda: "localhost"
    )
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "true")


def _runner_args(bundle: Path) -> Any:
    from cli.parsers import build_parser

    return build_parser().parse_args(
        [
            "init",
            "--serve-agent-runner",
            "--no-serve-gateway",
            "--machine-name",
            _MACHINE,
            "--gateway-url",
            "http://127.0.0.1:1",
            "--db-capability",
            str(bundle),
        ]
    )


def _issue_bundle(out: Path, runner: Path, capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    """Issue `out` for the unit at `runner`: (what `issue-unit` printed, its transport key)."""
    from cli.commands.cluster.control import cmd_db_authority_issue_unit

    assert (
        cmd_db_authority_issue_unit(machine=_MACHINE, home=str(runner), out=str(out), ttl_hours=1)
        == 0
    )
    printed = capsys.readouterr().out
    match = re.search(r"transport key \(shown once, carry it separately\): (\S+)", printed)
    assert match is not None, printed
    return printed, match.group(1)


def _install_on_initialized_unit(bundle: Path, key: str) -> None:
    """The initialized unit takes the next bundle through `install-unit`: the same
    join, the key from the environment, the bundle consumed."""
    from cli.commands.cluster.control import cmd_db_authority_install_unit

    os.environ[unit.CAPABILITY_KEY_ENV] = key
    assert cmd_db_authority_install_unit(bundle=str(bundle)) == 0
    assert not bundle.exists() and unit.CAPABILITY_KEY_ENV not in os.environ


def test_issued_bundle_starts_a_runner_that_connects_as_the_generation_login(
    born: Born, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve_on_loopback(monkeypatch, born)
    # The gateway's API is authenticated: the bundle carries the unit's API admission.
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _HUMAN)
    runner = (tmp_path / "runner").resolve()
    bundle = tmp_path / "mini.bundle"
    printed, key = _issue_bundle(bundle, runner, capsys)
    # A second bundle for the same unit and generation: what `install-unit` installs
    # on the already initialized unit below.
    renewal = tmp_path / "mini-renewal.bundle"
    _, renewal_key = _issue_bundle(renewal, runner, capsys)
    # The unit's first start precedes its `ava` link: the hint names the checkout's CLI.
    assert "`.venv/bin/ava init --db-capability <bundle>`" in printed, printed
    assert "`ava cluster db-authority install-unit <bundle>`" in printed, printed
    assert bundle.stat().st_mode & 0o777 == 0o600
    served = config.bootstrap_config_values()
    assert urlsplit(served["AVA_DB_URL"]).password is None

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(start_intent, "_checkout", lambda: checkout)

    bearers: list[object] = []

    def fetch(*_a: object, **kwargs: object) -> dict[str, str]:
        bearers.append(kwargs.get("bearer"))
        return dict(served)

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", fetch)
    with patch.dict(os.environ):
        os.environ.pop("AVA_MACHINE_SERVE_GATEWAY")
        # The unit joins without the human secret: its bundle authenticates it.
        os.environ.pop("AVA_CLUSTER_SECRET", None)
        os.environ["AVA_HOME"] = str(runner)
        os.environ[unit.CAPABILITY_KEY_ENV] = key
        prepare_init_identity(_runner_args(bundle))
        assert unit.CAPABILITY_KEY_ENV not in os.environ
        _install_on_initialized_unit(renewal, renewal_key)
    assert not bundle.exists()
    env = dotenv_values(runner / ".env")
    assert "AVA_DB_URL" not in env and unit.CAPABILITY_KEY_ENV not in env
    assert "AVA_CLUSTER_SECRET" not in env

    capability = unit.require_unit_capability(runner)
    # Each join's bootstrap fetch (init, then install-unit) presented the generation's
    # runner API token.
    assert capability.api is not None and bearers == [capability.api.token] * 2
    with psycopg.connect(capability.dsn, prepare_threshold=None, connect_timeout=5) as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ava_g0_runner",)
        assert conn.execute("SELECT count(*) FROM agents_meta").fetchone() is not None
    # The runner's launcher delivers exactly that login to its services. (A
    # scoped patch: the gateway fixture's teardown still acts on its own home.)
    with monkeypatch.context() as scoped:
        scoped.setenv("AVA_HOME", str(runner))
        scoped.setattr("base.host.env.bootstrap.config_source_is_local", lambda: False)
        assert bringup.db_delivery("runner") == {
            "AVA_DB_URL": capability.dsn,
            authority.GENERATION_ENV: "0",
        }


def test_a_remote_unit_home_holding_the_human_secret_refuses_to_join(tmp_path: Path) -> None:
    """A remote unit never holds the human secret; a home that still records
    it refuses before any fetch or identity effect."""
    home = (tmp_path / "legacy-runner").resolve()
    home.mkdir(mode=0o700)
    values = {"AVA_GATEWAY_URL": "http://10.0.0.7:8000", "AVA_CLUSTER_SECRET": _HUMAN}
    with pytest.raises(ValueError, match="records the human cluster secret"):
        unit_join.join_gateway(values, home, None)
