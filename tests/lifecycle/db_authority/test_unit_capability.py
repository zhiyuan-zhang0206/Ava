"""Per-unit database capability for remote agent-runners (the manual delivery).

Bootstrap serves no database login. The gateway operator issues a sealed,
unit-bound bundle of the ACTIVE write generation's runner login; the runner
installs it at start and its launcher delivers it. The single-box gateway fixture (real PostgreSQL 17, PgBouncer and
Redis) proves the end-to-end path, that a bearer-only runner receives nothing,
and that a revoked generation's bundle never installs. The tamper, binding and
boot-pass checks need no database.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from dotenv import dotenv_values

from base import config
from base.cluster import authority
from base.cluster.authority import fence, unit
from base.cluster.authority.api import API_TOKEN_ENV
from base.config import settings
from base.config.service_read import served_db_endpoint
from base.db.connections import NoDatabaseAuthorityError, _guard_db_url
from base.deploy.progress_timeout import UNIT_BUNDLE_MAX_TTL_S
from base.host.env import bootstrap, dotenv_boot
from cli import start_intent, unit_join
from cli.commands.data_plane import bringup
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.lifecycle.start_generation import _write_generation
from cli.commands.tests import test_single_box as _single_box
from cli.commands.tests.test_single_box import Born, _refused
from tests.lifecycle._init_identity import prepare_init_identity

# The single-box gateway fixtures (real PostgreSQL, PgBouncer and Redis),
# shared by name with this module.
configured = _single_box.configured
born = _single_box.born

_REPO = Path(__file__).resolve().parents[3]
_ENDPOINT = "postgresql://ava@10.0.0.7:6433/ava"
_MACHINE = "mini"
_HUMAN = "gateway-human-secret-" + "s" * 32


def _no_probe(_dsn: str) -> None:
    return None


def _refusing_probe(_dsn: str) -> None:
    raise AssertionError("an older bundle is refused before its login is probed")


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


# ── sealing: tampering, keys, expiry ────────────────────────────────────────


def _flip(text: str) -> str:
    return ("A" if text[0] != "A" else "B") + text[1:]


_MUTATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "ciphertext": lambda d: d.update(ciphertext=_flip(d["ciphertext"])),
    "truncated ciphertext": lambda d: d.update(ciphertext=d["ciphertext"][:-8]),
    "iv": lambda d: d.update(iv=_flip(d["iv"])),
    "header generation": lambda d: d["header"].update(generation=d["header"]["generation"] + 1),
    "header machine": lambda d: d["header"].update(machine="other"),
    "header home": lambda d: d["header"].update(home="/elsewhere"),
    "header expiry": lambda d: d["header"].update(expires_at=d["header"]["expires_at"] + 1e6),
}


@pytest.mark.parametrize("mutation", sorted(_MUTATIONS))
def test_a_tampered_bundle_is_refused(gateway: Path, runner_home: Path, mutation: str) -> None:
    issued = _issue(gateway, runner_home)
    assert _open(issued).capability.unit.home == str(runner_home)
    data = json.loads(issued.envelope)
    _MUTATIONS[mutation](data)
    with pytest.raises(unit.UnitCapabilityError, match="does not authenticate"):
        unit.open_bundle(json.dumps(data).encode(), issued.transport_key)
    assert unit.load_unit_capability(runner_home) is None


def test_a_bundle_opens_only_with_its_own_transport_key(gateway: Path, runner_home: Path) -> None:
    issued, other = _issue(gateway, runner_home), _issue(gateway, runner_home)
    assert issued.transport_key != other.transport_key
    with pytest.raises(unit.UnitCapabilityError, match="does not authenticate"):
        unit.open_bundle(issued.envelope, other.transport_key)
    with pytest.raises(unit.UnitCapabilityError, match="32-byte key"):
        unit.open_bundle(issued.envelope, "AAAA")
    with pytest.raises(unit.UnitCapabilityError, match="not a database capability bundle"):
        unit.open_bundle(b"{}", issued.transport_key)
    # The envelope never carries the login or the API token in clear.
    bundle = _open(issued)
    assert bundle.capability.api is not None
    for secret in (bundle.capability.password, bundle.capability.api.token):
        assert secret.encode() not in issued.envelope


def test_an_expired_bundle_is_refused(gateway: Path, runner_home: Path) -> None:
    issued = _issue(gateway, runner_home, ttl_s=60, now=time.time() - 120)
    with pytest.raises(unit.UnitCapabilityError, match="expired"):
        _open(issued)


@pytest.mark.parametrize(
    "ttl_s",
    [0.0, -1.0, math.nan, UNIT_BUNDLE_MAX_TTL_S + 1, math.inf],
    ids=["zero", "negative", "nan", "one-past-the-cap", "infinite"],
)
def test_issue_refuses_a_lifetime_outside_the_cap(
    gateway: Path, runner_home: Path, ttl_s: float
) -> None:
    with pytest.raises(unit.UnitCapabilityError, match="lifetime"):
        _issue(gateway, runner_home, ttl_s=ttl_s)


def test_issue_takes_up_to_the_capped_lifetime(gateway: Path, runner_home: Path) -> None:
    issued = _issue(gateway, runner_home, ttl_s=UNIT_BUNDLE_MAX_TTL_S, now=1_000_000.0)
    assert issued.expires_at == 1_000_000.0 + UNIT_BUNDLE_MAX_TTL_S


def test_issue_needs_an_active_generation(tmp_path: Path, runner_home: Path) -> None:
    bare = (tmp_path / "bare").resolve()
    bare.mkdir(mode=0o700)
    with pytest.raises(authority.AuthorityRefusedError, match="no database authority ledger"):
        _issue(bare, runner_home)


def test_issue_names_only_the_credential_free_endpoint(gateway: Path, runner_home: Path) -> None:
    with pytest.raises(unit.UnitCapabilityError, match="credential-free"):
        _issue(gateway, runner_home, endpoint="postgresql://ava:pw@10.0.0.7:6433/ava")


# ── install: binding, private store ─────────────────────────────────────────


def test_install_binds_the_unit_and_the_served_endpoint(
    gateway: Path, runner_home: Path, tmp_path: Path
) -> None:
    issued = _issue(gateway, runner_home)
    bundle = _open(issued)
    with pytest.raises(unit.UnitCapabilityError, match="issued for"):
        unit.install_bundle(
            runner_home, bundle, machine="other", served_endpoint=_ENDPOINT, probe=_no_probe
        )
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    with pytest.raises(unit.UnitCapabilityError, match="issued for"):
        unit.install_bundle(
            elsewhere, bundle, machine=_MACHINE, served_endpoint=_ENDPOINT, probe=_no_probe
        )
    with pytest.raises(unit.UnitCapabilityError, match="another database endpoint"):
        _install(runner_home, issued, served_endpoint="postgresql://ava@10.0.0.8:6433/ava")
    assert unit.load_unit_capability(runner_home) is None

    installed = _install(runner_home, issued)
    assert unit.unit_capability_path(runner_home).stat().st_mode & 0o777 == 0o600
    assert (unit.unit_capability_path(runner_home).parent.stat().st_mode & 0o777) == 0o700
    assert installed == unit.require_unit_capability(runner_home)
    assert installed.role == "ava_g0_runner"


# ── the runner's boot pass and launcher ─────────────────────────────────────


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
    monkeypatch.setattr(dotenv_boot, "_db_authority_refusal", None)
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


def _boot_with_bootstrap() -> None:
    """The runner's boot order: the authority pass, the unit delivery (which
    supplies the fetch's API token), then the bootstrap payload (the
    credential-free endpoint)."""
    dotenv_boot._enforce_cluster_env_authority(dotenv_boot.resolve_ava_home())
    dotenv_boot.deliver_unit_authority()
    bootstrap._apply_bootstrap_values(
        "http://gateway.invalid", {"AVA_DB_URL": _ENDPOINT, "AVA_EVENTS_CHANNEL": "ava:events"}
    )


def test_a_launched_service_keeps_its_unit_login_over_the_bootstrap_endpoint(
    runner_boot: unit.UnitCapability,
) -> None:
    os.environ["AVA_PROCESS_PROFILE"] = "runner"
    os.environ.update(unit.unit_delivery(Path(runner_boot.unit.home)))
    os.environ.update(unit.unit_api_delivery(Path(runner_boot.unit.home)))
    _boot_with_bootstrap()
    assert os.environ["AVA_DB_URL"] == runner_boot.dsn
    assert os.environ[authority.GENERATION_ENV] == "0"
    assert runner_boot.api is not None and os.environ[API_TOKEN_ENV] == runner_boot.api.token
    assert dotenv_boot.db_authority_refusal() is None


def test_a_remote_unit_drops_an_inherited_human_secret(
    runner_boot: unit.UnitCapability,
) -> None:
    """Its `.env` never declares the human secret, so a copy inherited from a
    shell is dropped: remote-unit processes never hold it."""
    del runner_boot
    os.environ["AVA_CLUSTER_SECRET"] = "inherited-" + "x" * 32
    _boot_with_bootstrap()
    assert "AVA_CLUSTER_SECRET" not in os.environ


def test_a_forged_login_is_replaced_by_the_endpoint_and_refused(
    runner_boot: unit.UnitCapability,
) -> None:
    del runner_boot
    os.environ["AVA_PROCESS_PROFILE"] = "runner"
    os.environ["AVA_DB_URL"] = "postgresql://ava_g0_runner:forged@10.0.0.7:6433/ava"
    os.environ[authority.GENERATION_ENV] = "0"
    _boot_with_bootstrap()
    assert os.environ["AVA_DB_URL"] == _ENDPOINT
    refusal = dotenv_boot.db_authority_refusal()
    assert refusal is not None and "only the root launcher delivers" in refusal
    with pytest.raises(NoDatabaseAuthorityError, match="credential-free"):
        _guard_db_url(_ENDPOINT)


def test_an_admitted_operator_process_consumes_the_installed_login(
    runner_boot: unit.UnitCapability, runner_home: Path
) -> None:
    intent = runner_home / "start-intent.json"
    intent.write_text(json.dumps({"home": str(runner_home), "checkout": str(_REPO)}))
    intent.chmod(0o600)
    _boot_with_bootstrap()
    assert os.environ["AVA_DB_URL"] == runner_boot.dsn
    assert os.environ[authority.GENERATION_ENV] == "0"
    # ... and its API token, which the bootstrap fetch then presents.
    assert runner_boot.api is not None and os.environ[API_TOKEN_ENV] == runner_boot.api.token
    assert dotenv_boot.db_authority_refusal() is None


def test_a_runner_without_a_capability_is_refused_by_name(
    runner_boot: unit.UnitCapability, runner_home: Path
) -> None:
    del runner_boot
    intent = runner_home / "start-intent.json"
    intent.write_text(json.dumps({"home": str(runner_home), "checkout": str(_REPO)}))
    intent.chmod(0o600)
    unit.unit_capability_path(runner_home).unlink()
    _boot_with_bootstrap()
    assert os.environ["AVA_DB_URL"] == _ENDPOINT
    assert API_TOKEN_ENV not in os.environ
    refusal = dotenv_boot.db_authority_refusal()
    assert refusal is not None and "holds no database capability" in refusal
    assert "ava cluster db-authority issue-unit" in refusal
    with pytest.raises(NoDatabaseAuthorityError, match="issue-unit"):
        _guard_db_url(_ENDPOINT)


def test_a_pure_runners_launcher_delivers_the_installed_capability(
    runner_boot: unit.UnitCapability, runner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVA_HOME", str(runner_home))
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", lambda: False)
    assert bringup.db_delivery("runner") == {
        "AVA_DB_URL": runner_boot.dsn,
        authority.GENERATION_ENV: "0",
    }
    with pytest.raises(RuntimeError, match="cannot launch a gateway-class"):
        bringup.db_delivery("gateway")
    # The launch digest binds the capability's non-secret reference.
    assert _write_generation(runner_home) == runner_boot.reference
    unit.unit_capability_path(runner_home).unlink()
    assert _write_generation(runner_home) is None
    with pytest.raises(unit.UnitCapabilityError, match="holds no database capability"):
        bringup.db_delivery("runner")


# ── real PostgreSQL + PgBouncer + Redis: the gateway fixture ────────────────


def _serve_on_loopback(monkeypatch: pytest.MonkeyPatch, born: Born) -> None:
    """Serve bootstrap from `born`'s `.env`. The single-box gateway binds
    loopback (empty bearer), so its served endpoint must stay on loopback for
    the co-located runner under test."""
    from base.host.env import runtime_config

    monkeypatch.setattr(runtime_config, "_ava_home", lambda: born.home)
    monkeypatch.setattr(config, "_self_machine_host", lambda: "localhost")
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
