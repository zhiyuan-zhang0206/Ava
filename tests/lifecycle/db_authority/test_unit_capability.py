"""Per-unit database capability for remote agent-runners (the manual delivery).

Bootstrap serves no database login. The gateway operator issues a sealed,
unit-bound bundle of the ACTIVE write generation's runner login plus the
unit's enrollment secret; the runner installs it at start and its launcher
delivers it. The single-box gateway fixture (real PostgreSQL 17, PgBouncer and
Redis) proves the end-to-end path, that a bearer-only runner receives nothing,
that a revoked generation's bundle never installs, and the networked cutover
step. The tamper, binding and boot-pass checks need no database.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from dotenv import dotenv_values

from cli import start_intent
from cli.commands.data_plane import bringup
from cli.commands.data_plane import cluster_instance as ci
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.lifecycle.start_generation import _write_generation
from scripts import cutover_db_authority as cutover
from shared import config
from shared.cluster import authority
from shared.cluster.authority import unit
from shared.cluster.authority.api import API_TOKEN_ENV
from shared.config import settings
from shared.config.service_read import served_db_endpoint
from shared.db.connections import NoDatabaseAuthorityError, _guard_db_url
from shared.deploy.progress_timeout import UNIT_BUNDLE_MAX_TTL_S
from shared.host.env import bootstrap, dotenv_boot
from tests.lifecycle._start_identity import prepare_start_identity
from tests.lifecycle.db_authority import test_single_box as _single_box
from tests.lifecycle.db_authority.test_single_box import Born, _refused

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
    # The envelope never carries the login, the API token or the enrollment
    # secret in clear.
    bundle = _open(issued)
    assert bundle.capability.api is not None
    for secret in (
        bundle.capability.password,
        bundle.capability.api.token,
        bundle.enrollment.secret,
    ):
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


# ── install: binding, enrollment, private store ─────────────────────────────


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
    for path in (unit.unit_capability_path(runner_home), unit.unit_enrollment_path(runner_home)):
        assert path.stat().st_mode & 0o777 == 0o600
    assert (unit.unit_capability_path(runner_home).parent.stat().st_mode & 0o777) == 0o700
    assert installed == unit.require_unit_capability(runner_home)
    assert installed.role == "ava_g0_runner"


def test_the_enrollment_is_minted_once_and_carried_to_the_unit(
    gateway: Path, runner_home: Path
) -> None:
    first, second = _open(_issue(gateway, runner_home)), _open(_issue(gateway, runner_home))
    assert first.enrollment == second.enrollment
    assert first.capability.bundle != second.capability.bundle
    record = unit.enrollment_record_path(gateway, first.capability.unit)
    assert record.stat().st_mode & 0o777 == 0o600
    assert unit.Enrollment.model_validate_json(record.read_bytes()) == first.enrollment
    unit.install_bundle(
        runner_home, first, machine=_MACHINE, served_endpoint=_ENDPOINT, probe=_no_probe
    )
    installed = unit.unit_enrollment_path(runner_home).read_bytes()
    assert unit.Enrollment.model_validate_json(installed) == first.enrollment


# ── the runner's boot pass and launcher ─────────────────────────────────────


@pytest.fixture
def runner_boot(
    monkeypatch: pytest.MonkeyPatch, runner_home: Path, gateway: Path
) -> Iterator[unit.UnitCapability]:
    """A pure agent-runner home (no database URL in `.env`) with an installed
    capability, as this process's anchored home. The whole environment is
    restored afterwards."""
    saved = dict(os.environ)
    env_file = runner_home / ".env"
    env_file.write_text("AVA_MACHINE_SERVE_AGENT_RUNNER=true\n")
    monkeypatch.setattr(dotenv_boot, "_HOME", runner_home)
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", True)
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", env_file)
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", runner_home / "absent-mirror.env")
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
    dotenv_boot._enforce_cluster_env_authority()
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
    monkeypatch.setattr(settings.general, "ava_home", str(runner_home))
    monkeypatch.setattr("shared.host.env.bootstrap.config_source_is_local", lambda: False)
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
    from shared.host.env import runtime_config

    monkeypatch.setattr(runtime_config, "_ava_home", lambda: born.home)
    monkeypatch.setattr(config, "_self_machine_host", lambda: "localhost")
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "true")


def _runner_args(bundle: Path) -> Any:
    from cli.parsers import build_parser

    return build_parser().parse_args(
        [
            "start",
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


def test_issued_bundle_starts_a_runner_that_connects_as_the_generation_login(
    born: Born, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.cluster.control import cmd_db_authority_issue_unit

    _serve_on_loopback(monkeypatch, born)
    # The gateway's API is authenticated: the bundle carries the unit's API admission.
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _HUMAN)
    runner = (tmp_path / "runner").resolve()
    bundle = tmp_path / "mini.bundle"
    assert (
        cmd_db_authority_issue_unit(
            machine=_MACHINE, home=str(runner), out=str(bundle), ttl_hours=1
        )
        == 0
    )
    printed = capsys.readouterr().out
    match = re.search(r"transport key \(shown once, carry it separately\): (\S+)", printed)
    assert match is not None, printed
    # The unit's first start precedes its $AVA_HOME/ava link: the hint names the checkout's CLI.
    assert "`.venv/bin/ava start --db-capability <bundle>`" in printed, printed
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
        os.environ["AVA_CLUSTER_REGISTRY"] = str(tmp_path / "runner-registry.json")
        os.environ[unit.CAPABILITY_KEY_ENV] = match.group(1)
        prepare_start_identity(_runner_args(bundle))
        assert unit.CAPABILITY_KEY_ENV not in os.environ
    assert not bundle.exists()
    env = dotenv_values(runner / ".env")
    assert "AVA_DB_URL" not in env and unit.CAPABILITY_KEY_ENV not in env
    assert "AVA_CLUSTER_SECRET" not in env

    capability = unit.require_unit_capability(runner)
    # The join's bootstrap fetch presented the bundle's runner API token.
    assert capability.api is not None and bearers == [capability.api.token]
    with psycopg.connect(capability.dsn, prepare_threshold=None, connect_timeout=5) as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ava_g0_runner",)
        assert conn.execute("SELECT count(*) FROM agents_meta").fetchone() is not None
    # The runner's launcher delivers exactly that login to its services. (A
    # scoped patch: the gateway fixture's teardown still acts on its own home.)
    with monkeypatch.context() as scoped:
        scoped.setattr(settings.general, "ava_home", str(runner))
        scoped.setattr("shared.host.env.bootstrap.config_source_is_local", lambda: False)
        assert bringup.db_delivery("runner") == {
            "AVA_DB_URL": capability.dsn,
            authority.GENERATION_ENV: "0",
        }


def test_a_remote_unit_home_holding_the_human_secret_refuses_to_start(tmp_path: Path) -> None:
    """A remote unit never holds the human secret; a home that still records
    it (not adopted) refuses before any fetch or identity effect."""
    home = (tmp_path / "legacy-runner").resolve()
    home.mkdir(mode=0o700)
    values = {"AVA_GATEWAY_URL": "http://10.0.0.7:8000", "AVA_CLUSTER_SECRET": _HUMAN}
    with pytest.raises(ValueError, match="records the human cluster secret"):
        start_intent._join(values, home, None)


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
        authority.revoke(conn, born.home, operation)
        authority.close_revoked(conn, born.home, operation)
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


# ── the networked cutover step ──────────────────────────────────────────────


def _redis_shutdown(port: int, admin_password: str) -> None:
    subprocess.run(  # noqa: S603 — the home-owned redis-cli, admin via env
        [ci._redis_cli_bin(), "-p", str(port), "shutdown", "nosave"],
        env=ci._redis_cli_env(admin_password),
        check=False,
        capture_output=True,
    )


_MINI, _WIN = ("mini", "/Users/u/.ava"), ("win", "C:\\Users\\u\\.ava")


@pytest.fixture
def networked(
    born: Born, monkeypatch: pytest.MonkeyPatch, set_machine_identity: Callable[..., None]
) -> Born:
    """The gateway fixture with two remote units registered; `win` is paused."""
    _serve_on_loopback(monkeypatch, born)
    # At the identity source: an earlier test may already have cached a name.
    set_machine_identity(role="gateway", name="gw")
    with born.admin() as conn:
        for machine, home in (("gw", str(born.home)), _MINI, _WIN):
            conn.execute(
                "INSERT INTO machine_units (machine_name, home) VALUES (%s, %s)", (machine, home)
            )
        conn.execute("INSERT INTO machines (name, paused_at) VALUES ('win', now())")
    return born


@pytest.mark.parametrize(
    ("plan", "match"),
    [
        (cutover.UnitPlan(), "unclassified"),
        (cutover.UnitPlan(include=(_MINI, _WIN), bundle_dir=Path("/unused")), "paused machines"),
        (cutover.UnitPlan(include=(_MINI,), exclude=(_WIN,)), "--bundle-dir"),
        (cutover.UnitPlan(include=(_MINI,), exclude=(_WIN, ("ghost", "/h"))), "unknown"),
    ],
)
def test_networked_cutover_refuses_an_unclassified_or_paused_unit(
    networked: Born, plan: cutover.UnitPlan, match: str
) -> None:
    before = (networked.home / ".env").read_bytes()
    with pytest.raises(cutover.CutoverRefusedError, match=match):
        cutover.convert_remote_units(networked.home, networked.record, plan, execute=True)
    assert "remote-units" not in cutover.read_journal(networked.home)
    assert (networked.home / ".env").read_bytes() == before


def _assert_redis_admin_rotated(born: Born, old_admin: str) -> str:
    redis_port = born.record.ports["redis"]
    new_admin = dotenv_values(born.home / ".env")["AVA_REDIS_ADMIN_PASSWORD"]
    assert new_admin and new_admin != old_admin
    assert not cutover._authenticates(redis_port, old_admin)
    assert cutover._authenticates(redis_port, new_admin)
    assert f'requirepass "{new_admin}"' in (ci.redis_data_dir() / "redis.conf").read_text()
    assert not (born.home / "db-authority" / "redis-admin.pending").exists()
    return new_admin


def _assert_redis_runtime_rotated(born: Born, old_runtime: str) -> None:
    """Runner homes, their residue and the W3 copies hold the runtime password too:
    it must stop authenticating, and the URL bootstrap serves carries the new one."""
    redis_port = born.record.ports["redis"]
    values = dotenv_values(born.home / ".env")
    url = urlsplit(values["AVA_REDIS_URL"] or "")
    user, new_runtime = url.username, values["AVA_REDIS_PASSWORD"]
    assert user and new_runtime and new_runtime != old_runtime
    assert url.password == new_runtime
    assert not cutover._authenticates(redis_port, old_runtime, user)
    assert cutover._authenticates(redis_port, new_runtime, user)
    assert not (born.home / "db-authority" / "redis-runtime.pending").exists()


def _assert_one_bundle_for_mini(outcome: str, bundles: Path) -> None:
    [(path, key)] = re.findall(r"(\S+\.bundle) transport key (\S+)", outcome)
    assert Path(path).parent == bundles
    assert bundles.stat().st_mode & 0o777 == 0o700
    assert [p.name.split("-")[0] for p in bundles.iterdir()] == ["mini"]
    issued = unit.open_bundle(Path(path).read_bytes(), key)
    assert issued.capability.unit == unit.UnitIdentity(machine=_MINI[0], home=_MINI[1])
    assert issued.capability.generation.number == 0


def test_networked_cutover_rotates_both_redis_passwords_and_issues_one_bundle_per_unit(
    networked: Born, tmp_path: Path
) -> None:
    born = networked
    redis_port = born.record.ports["redis"]
    old_admin, old_runtime = (
        born.values["AVA_REDIS_ADMIN_PASSWORD"],
        born.values["AVA_REDIS_PASSWORD"],
    )
    bundles = tmp_path / "bundles"
    classified = cutover.UnitPlan(include=(_MINI,), exclude=(_WIN,), bundle_dir=bundles)
    dry = cutover.convert_remote_units(born.home, born.record, classified, execute=False)
    assert dry.startswith("remote-units: would rotate")
    assert cutover._authenticates(redis_port, old_admin)
    try:
        outcome = cutover.convert_remote_units(born.home, born.record, classified, execute=True)
        new_admin = _assert_redis_admin_rotated(born, old_admin)
        _assert_redis_runtime_rotated(born, old_runtime)
        assert cutover.read_journal(born.home)["remote-units"] == "done"
        _assert_one_bundle_for_mini(outcome, bundles)
        # A repeat is a verified no-op: no second rotation, no new bundles.
        repeat = cutover.convert_remote_units(born.home, born.record, classified, execute=True)
        assert repeat.startswith("remote-units: verified")
        assert dotenv_values(born.home / ".env")["AVA_REDIS_ADMIN_PASSWORD"] == new_admin
    finally:
        _redis_shutdown(
            redis_port, dotenv_values(born.home / ".env")["AVA_REDIS_ADMIN_PASSWORD"] or ""
        )


def test_an_interrupted_rotation_resumes_with_the_staged_passwords(
    networked: Born, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis took both new passwords, then `.env` failed to record them: the re-run
    applies the same staged values instead of minting others."""
    born = networked
    redis_port = born.record.ports["redis"]
    staged = born.home / "db-authority"
    plan = cutover.UnitPlan(include=(_MINI,), exclude=(_WIN,), bundle_dir=tmp_path / "bundles")
    upsert = cutover.upsert_env
    monkeypatch.setattr(cutover, "upsert_env", MagicMock(side_effect=OSError("disk full")))
    try:
        with pytest.raises(OSError, match="disk full"):
            cutover.convert_remote_units(born.home, born.record, plan, execute=True)
        admin, runtime = (
            (staged / f"redis-{name}.pending").read_text().strip() for name in ("admin", "runtime")
        )
        assert cutover.read_journal(born.home)["remote-units"] == "issuing"
        monkeypatch.setattr(cutover, "upsert_env", upsert)
        cutover.convert_remote_units(born.home, born.record, plan, execute=True)
        values = dotenv_values(born.home / ".env")
        assert (values["AVA_REDIS_ADMIN_PASSWORD"], values["AVA_REDIS_PASSWORD"]) == (
            admin,
            runtime,
        )
        assert not list(staged.glob("redis-*.pending"))
    finally:
        for password in {born.values["AVA_REDIS_ADMIN_PASSWORD"], _env_admin(born)}:
            _redis_shutdown(redis_port, password)


def _env_admin(born: Born) -> str:
    pending = born.home / "db-authority" / "redis-admin.pending"
    if pending.exists():
        return pending.read_text().strip()
    return dotenv_values(born.home / ".env")["AVA_REDIS_ADMIN_PASSWORD"] or ""


def test_a_single_box_has_no_remote_units(
    born: Born, set_machine_identity: Callable[..., None]
) -> None:
    set_machine_identity(role="gateway", name="gw")
    with born.admin() as conn:
        conn.execute(
            "INSERT INTO machine_units (machine_name, home) VALUES ('gw', %s)", (str(born.home),)
        )
    outcome = cutover.convert_remote_units(born.home, born.record, cutover.UnitPlan(), execute=True)
    assert outcome == "remote-units: none (single box)"
    assert cutover._authenticates(
        born.record.ports["redis"], born.values["AVA_REDIS_ADMIN_PASSWORD"]
    )


def test_networked_cutover_rotates_the_bearer_once_before_issuing_bundles(
    networked: Born, tmp_path: Path
) -> None:
    """Step `api`: the human secret every runner held rotates once, after the
    logical-backup passphrase is pinned; the bundles issued afterwards carry the
    rotated telemetry token and the generation's API admission."""
    from scripts.data_plane_ops import rotate_cluster_secret as bearer
    from services.gateway_side.backup import passphrase
    from shared.cluster.authority.api import telemetry_token
    from shared.host.env.dotenv_file import upsert_env

    born = networked
    upsert_env(born.home / ".env", {"AVA_CLUSTER_SECRET": _HUMAN}, audit_site="test")
    # A home born before births minted a passphrase: it encrypted under sha256(secret).
    passphrase.pin_path(born.home).unlink()
    dry = cutover.convert_api(born.home, born.record, execute=False)
    assert dry.startswith("api: would pin the logical-backup passphrase and rotate")
    assert dotenv_values(born.home / ".env")["AVA_CLUSTER_SECRET"] == _HUMAN

    outcome = cutover.convert_api(born.home, born.record, execute=True)
    rotated = dotenv_values(born.home / ".env")["AVA_CLUSTER_SECRET"] or ""
    assert outcome.startswith("api: AVA_CLUSTER_SECRET rotated once") and rotated != _HUMAN
    assert passphrase.pinned(born.home) == passphrase.derive(_HUMAN)
    journal = json.loads(cutover.journal_path(born.home).read_text())
    assert journal["steps"]["api"] == "done" and journal["api"]["state"] == "done"
    assert _HUMAN not in json.dumps(journal) and rotated not in json.dumps(journal)
    assert bearer.bearer_fingerprint(rotated) == journal["api"]["new"]
    # A repeat verifies; it never rotates a second time.
    assert cutover.convert_api(born.home, born.record, execute=True).startswith("api: AVA")
    assert dotenv_values(born.home / ".env")["AVA_CLUSTER_SECRET"] == rotated

    bundles = tmp_path / "bundles"
    plan = cutover.UnitPlan(include=(_MINI,), exclude=(_WIN,), bundle_dir=bundles)
    try:
        outcome = cutover.convert_remote_units(born.home, born.record, plan, execute=True)
    finally:
        _redis_shutdown(
            born.record.ports["redis"],
            dotenv_values(born.home / ".env")["AVA_REDIS_ADMIN_PASSWORD"] or "",
        )
    [(path, key)] = re.findall(r"(\S+\.bundle) transport key (\S+)", outcome)
    api = unit.open_bundle(Path(path).read_bytes(), key).capability.api
    secret = authority.read_secret(born.home, authority.active_generation(born.home))
    assert api is not None and api.token == secret.api.runner
    assert api.telemetry == telemetry_token(rotated) != telemetry_token(_HUMAN)


def test_a_single_box_keeps_its_bearer_and_pins_its_backup_passphrase(
    born: Born, set_machine_identity: Callable[..., None]
) -> None:
    """A home born before births minted a passphrase, with an empty secret: the
    step pins a minted passphrase (never the public sha256(""))."""
    from services.gateway_side.backup import passphrase

    passphrase.pin_path(born.home).unlink()
    set_machine_identity(role="gateway", name="gw")
    assert cutover.convert_api(born.home, born.record, execute=False).startswith("api: would pin")
    assert passphrase.pinned(born.home) is None
    outcome = cutover.convert_api(born.home, born.record, execute=True)
    assert outcome.startswith("api: single box keeps its bearer")
    pinned = passphrase.pinned(born.home)
    assert pinned is not None and pinned != passphrase.LEGACY_EMPTY_SECRET_PASSPHRASE
    assert dotenv_values(born.home / ".env")["AVA_CLUSTER_SECRET"] == ""
    assert "api" not in cutover.read_journal(born.home)
