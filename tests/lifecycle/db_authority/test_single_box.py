"""Single-box birth and ordinary start on the always-authenticated data plane.

Real Postgres 17, PgBouncer and Redis under a temp `$AVA_HOME`, driven through
the official start steps (`ensure_gateway_data_plane` -> `prepare_gateway_schema`
-> `cmd_migrations_apply` -> `complete_gateway_data_plane`) with an EMPTY
cluster secret — the single-box default that used to mean "no credentials".

A fresh birth must leave: a NOLOGIN, password-less schema owner; the two
capability groups; write generation 0 active in the private ledger; a pooler
serving exactly that pair with SCRAM (plus the admin console entry); and no
passwordless door for anything but the OS user over the owner-only socket.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql

from cli.commands import _cluster_instance as ci
from cli.commands import _data_plane as data_plane
from cli.commands import _pgbouncer as pooler
from cli.commands.migrations import cmd_migrations_apply
from shared import cluster
from shared.cluster import authority, ownership
from shared.config import settings
from shared.url_secret import url_with_userinfo
from tests._containers import _free_port

pytestmark = pytest.mark.skipif(
    not (Path(pooler.pgbouncer_bin()).exists() or shutil.which(pooler.pgbouncer_bin())),
    reason="pgbouncer not installed (brew/apt)",
)

_REPO = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Born:
    home: Path
    record: cluster.ClusterRecord
    values: dict[str, str]

    @property
    def pg_port(self) -> int:
        return self.record.ports["postgres"]

    @property
    def pooler_port(self) -> int:
        return cluster.record_pgbouncer_port(self.record)

    def endpoint(self) -> str:
        return self.values["AVA_DB_URL"]

    def login(self, cls: str) -> tuple[str, str]:
        secret = authority.read_secret(self.home, authority.active_generation(self.home))
        role = secret.roles.of(cast("Any", cls))
        return role.name, role.password

    def dsn(self, cls: str) -> str:
        name, password = self.login(cls)
        return url_with_userinfo(self.endpoint(), name, password)

    def admin(self) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host=str(ci._pg_socket_dir()), port=self.pg_port, dbname="ava", autocommit=True
        )


def _intent(home: Path, record: cluster.ClusterRecord, values: dict[str, str]) -> None:
    body = {
        "version": 1,
        "home": str(home),
        "checkout": str(_REPO),
        "worktree": False,
        "roles": ["agent-runner", "gateway"],
        "config_digest": None,
        "phase": "configured",
        "record": asdict(record),
        "env": values,
    }
    path = home / "start-intent.json"
    path.write_text(json.dumps(body, sort_keys=True) + "\n")
    path.chmod(0o600)


def _teardown(home: Path, redis_port: int, admin_password: str) -> None:
    try:
        pooler.stop_pgbouncer(force=True)
    finally:
        subprocess.run(  # noqa: S603 — resolved pg_ctl + test-owned data dir
            [ci._pg_bin("pg_ctl"), "-D", str(home / "pg"), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
        subprocess.run(  # noqa: S603 — the home-owned redis-cli, admin via env
            [ci._redis_cli_bin(), "-p", str(redis_port), "shutdown", "nosave"],
            env=ci._redis_cli_env(admin_password),
            check=False,
            capture_output=True,
        )


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Born:
    """A configured-but-unprovisioned single-box home (empty cluster secret)."""
    from cli.start_identity import IdentityInput, _gateway_values

    home = (tmp_path / "home").resolve()
    home.mkdir(mode=0o700)
    monkeypatch.setattr(settings.general, "ava_home", str(home))
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))
    ports = dict(cluster.LEGACY_AVA_PORTS)
    ports.update(postgres=_free_port(), redis=_free_port(), pgbouncer=_free_port())
    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", ports), gateway_home=str(home), created_at="test"
    )
    values = _gateway_values(
        record,
        IdentityInput(
            home,
            tmp_path / "clusters.json",
            _REPO,
            False,
            frozenset({"gateway", "agent-runner"}),
            {},
        ),
    )
    assert values["AVA_CLUSTER_SECRET"] == ""
    assert "AVA_DB_ADMIN_PASSWORD" not in values
    assert "AVA_RUNNER_DB_PASSWORD" not in values
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    _intent(home, record, values)
    dp = settings.data_plane
    monkeypatch.setattr(dp, "cluster_secret", "")
    monkeypatch.setattr(dp, "db_url", values["AVA_DB_URL"])
    monkeypatch.setattr(dp, "redis_url", values["AVA_REDIS_URL"])
    monkeypatch.setattr(dp, "redis_admin_password", values["AVA_REDIS_ADMIN_PASSWORD"])
    monkeypatch.setattr(dp, "events_channel", values["AVA_EVENTS_CHANNEL"])
    monkeypatch.setattr(dp, "pgbouncer_enabled", True)
    # The start process adopts its delivered login into os.environ; the raw-env
    # seam restores the suite's own values at teardown.
    monkeypatch.setitem(os.environ, "AVA_DB_URL", values["AVA_DB_URL"])
    monkeypatch.delitem(os.environ, authority.GENERATION_ENV, raising=False)

    def _record(_home: Path) -> cluster.ClusterRecord:
        return record

    monkeypatch.setattr(cluster, "get_record", _record)
    return Born(home=home, record=record, values=values)


def _birth(born: Born) -> None:
    assert data_plane.ensure_gateway_data_plane() == 0
    data_plane.prepare_gateway_schema()
    cmd_migrations_apply()
    data_plane.complete_gateway_data_plane()


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Born]:
    born = _configure(monkeypatch, tmp_path)
    try:
        yield born
    finally:
        _teardown(born.home, born.record.ports["redis"], born.values["AVA_REDIS_ADMIN_PASSWORD"])


@pytest.fixture
def born(configured: Born) -> Born:
    _birth(configured)
    return configured


# A login without a stored verifier fails authentication before PostgreSQL
# even reaches its LOGIN check; PgBouncer reports its own SCRAM failure.
_AUTH_REFUSED = (
    "password authentication failed",
    "not permitted to log in",
    "SASL authentication failed",
    "no password supplied",
)


def _refused(**kwargs: Any) -> str:
    with pytest.raises(psycopg.OperationalError) as excinfo:
        psycopg.connect(connect_timeout=5, **kwargs).close()
    message = str(excinfo.value)
    assert any(marker in message for marker in _AUTH_REFUSED), message
    return message


def test_fresh_birth_mints_generation_zero_behind_nologin_owner(born: Born) -> None:
    ledger = authority.require_ledger(born.home)
    assert ledger.owner == "ava"
    assert ledger.active is not None and ledger.active.number == 0
    assert ledger.active.origin.kind == "birth"
    with born.admin() as conn:
        owner = conn.execute(
            "SELECT rolcanlogin, rolpassword IS NULL, rolsuper FROM pg_authid WHERE rolname = 'ava'"
        ).fetchone()
        assert owner == (False, True, False)
        groups = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname IN ('ava_gateway', 'ava_runner')"
            " AND NOT rolcanlogin ORDER BY 1"
        ).fetchall()
        assert groups == [("ava_gateway",), ("ava_runner",)]
        authority.check_invariant(
            conn, born.home, database="ava", readonly_grantees=data_plane.READONLY_GRANTEES
        )
    # The start process adopted the delivered gateway login for its own dials.
    assert settings.data_plane.db_url == born.dsn("gateway")
    assert os.environ[authority.GENERATION_ENV] == "0"
    assert json.loads((born.home / "start-intent.json").read_text())["phase"] == "provisioned"


def test_birth_refuses_unauthenticated_and_owner_logins(born: Born) -> None:
    socket_dir = str(ci._pg_socket_dir())
    for host in ("127.0.0.1", socket_dir):
        # The owner exists but can never log in, with or without a password.
        for password in ("", "anything"):
            _refused(host=host, port=born.pg_port, user="ava", password=password, dbname="ava")
    # No password-less door over TCP, even for the OS-user superuser.
    _refused(host="127.0.0.1", port=born.pg_port, user=getpass.getuser(), password="", dbname="ava")
    # The pooler admits neither the owner nor a login without its password.
    _refused(host="127.0.0.1", port=born.pooler_port, user="ava", password="x", dbname="ava")  # noqa: S106 — a wrong credential by design
    name, _password = born.login("gateway")
    _refused(host="127.0.0.1", port=born.pooler_port, user=name, password="wrong", dbname="ava")  # noqa: S106 — a wrong credential by design
    # The OS user reaches the administrator only through peer on the owner-only socket.
    with born.admin() as conn:
        assert conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = session_user"
        ).fetchone() == (True,)


def test_generation_logins_work_with_group_privileges(born: Born) -> None:
    with psycopg.connect(born.dsn("gateway"), prepare_threshold=None, autocommit=True) as gw:
        assert gw.execute("SELECT session_user").fetchone() == (born.login("gateway")[0],)
        gw.execute("INSERT INTO agents (id) VALUES (910001)")
        gw.execute("INSERT INTO agents_meta (id, status) VALUES (910001, 'running')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gw.execute("CREATE TABLE gateway_must_not_create (id int)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gw.execute("SET ROLE ava")
    with psycopg.connect(born.dsn("runner"), prepare_threshold=None, autocommit=True) as runner:
        assert runner.execute("SELECT count(*) FROM agents WHERE id = 910001").fetchone() == (1,)
        runner.execute("UPDATE agents_meta SET status = 'idling' WHERE id = 910001")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runner.execute("INSERT INTO agents (id) VALUES (910002)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runner.execute("SET ROLE ava_gateway")


def test_pooler_admin_console_is_the_userlist_only_operator_entry(born: Born) -> None:
    admin = authority.read_pooler_admin(born.home)
    assert pooler.pgbouncer_listener_reachable(born.pooler_port, admin.password)
    assert not pooler.pgbouncer_listener_reachable(born.pooler_port, "wrong-password-xxxxx")
    userlist = (born.home / "pgbouncer" / "userlist.txt").read_bytes()
    assert userlist == authority.render_userlist(born.home, authority.active_generation(born.home))
    names = [line.split('"')[1] for line in userlist.decode().splitlines()]
    assert names == [*born.login("gateway")[:1], *born.login("runner")[:1], authority.POOLER_ADMIN]
    assert b"SCRAM-SHA-256$" in userlist
    assert admin.password.encode() not in userlist


def test_pooler_restart_revokes_a_removed_user_a_reload_would_keep(born: Born) -> None:
    """E11: after a SIGHUP PgBouncer still admits a removed user that had
    authenticated; changed userlist bytes therefore restart the pooler."""
    name, password = born.login("gateway")
    with psycopg.connect(born.dsn("gateway"), prepare_threshold=None) as conn:
        conn.execute("SELECT 1")
    owner_before = ownership.pooler(pooler._ini_path(), pooler._pidfile_path())
    assert owner_before is not None
    admin = authority.read_pooler_admin(born.home)
    userlist = authority.render_userlist(born.home, authority.active_generation(born.home))
    without_gateway = b"".join(
        line
        for line in userlist.splitlines(keepends=True)
        if not line.startswith(f'"{name}"'.encode())
    )
    rc = pooler.ensure_pgbouncer(
        pg_port=born.pg_port,
        listen_port=born.pooler_port,
        db_name="ava",
        cluster_secret="",
        userlist=without_gateway,
        admin_password=admin.password,
    )
    assert rc == 0
    owner_after = ownership.pooler(pooler._ini_path(), pooler._pidfile_path())
    assert owner_after is not None and owner_after.pid != owner_before.pid
    _refused(host="127.0.0.1", port=born.pooler_port, user=name, password=password, dbname="ava")


def test_unchanged_userlist_reloads_without_restart(born: Born) -> None:
    before = ownership.pooler(pooler._ini_path(), pooler._pidfile_path())
    data_plane.complete_gateway_data_plane()
    after = ownership.pooler(pooler._ini_path(), pooler._pidfile_path())
    assert before is not None and after is not None and before.pid == after.pid


def test_ordinary_start_sweeps_a_stale_login_and_keeps_the_generation(born: Born) -> None:
    with born.admin() as conn:
        conn.execute(
            "CREATE ROLE ava_g7_runner LOGIN PASSWORD 'stale-password-xyz' IN ROLE ava_runner"
        )
    data_plane.complete_gateway_data_plane()
    with born.admin() as conn:
        assert conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'ava_g7_runner'"
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member"
            " WHERE r.rolname = 'ava_g7_runner'"
        ).fetchone() == (0,)
    assert authority.active_generation(born.home).number == 0


def test_ordinary_start_holds_on_an_invariant_violation(born: Born) -> None:
    with born.admin() as conn:
        conn.execute("CREATE ROLE foreign_writer LOGIN PASSWORD 'foreign-password'")
        conn.execute("GRANT INSERT ON agents TO foreign_writer")
    with pytest.raises(authority.CatalogRefusedError, match="foreign_writer"):
        data_plane.complete_gateway_data_plane()


def test_ordinary_start_refuses_a_home_without_a_ledger_before_any_effect(
    configured: Born, capsys: pytest.CaptureFixture[str]
) -> None:
    intent = json.loads((configured.home / "start-intent.json").read_text())
    intent["phase"] = "provisioned"
    (configured.home / "start-intent.json").write_text(json.dumps(intent))
    assert data_plane.ensure_gateway_data_plane() == 1
    assert "cutover_db_authority.py" in capsys.readouterr().err
    assert not (configured.home / "pg").exists()
    with pytest.raises(RuntimeError, match="cutover_db_authority"):
        data_plane.complete_gateway_data_plane()


def test_interrupted_birth_retries_to_the_same_generation(
    configured: Born, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = data_plane.prove_generation_logins

    def failing(home: Path, generation: authority.Generation, endpoint: str) -> None:
        calls.append(generation.number)
        raise RuntimeError("injected crash before activation")

    monkeypatch.setattr(data_plane, "prove_generation_logins", failing)
    with pytest.raises(RuntimeError, match="injected crash"):
        _birth(configured)
    ledger = authority.require_ledger(configured.home)
    assert ledger.active is None and ledger.pending is not None and ledger.pending.number == 0
    monkeypatch.setattr(data_plane, "prove_generation_logins", real)
    data_plane.complete_gateway_data_plane()
    ledger = authority.require_ledger(configured.home)
    assert calls == [0] and ledger.active is not None and ledger.active.number == 0
    assert ledger.counter == 0


def test_launched_services_receive_their_class_login_only(
    born: Born, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands._root_driver import _root_child_env, _service_extra_env
    from ops.service_spec import ServiceSpec

    # The launcher's settings-free serve-gateway read (a born single box).
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", "true")

    gateway = ServiceSpec(
        session="gateway", cmd="x", capabilities=frozenset({"gateway"}), requires_db=True
    )
    runner = ServiceSpec(
        session="ops", cmd="x", capabilities=frozenset({"agent-runner"}), requires_db=True
    )
    frontend = ServiceSpec(
        session="frontend", cmd="x", capabilities=frozenset({"gateway"}), requires_db=False
    )
    assert _service_extra_env(gateway)["AVA_DB_URL"] == born.dsn("gateway")
    assert _service_extra_env(runner)["AVA_DB_URL"] == born.dsn("runner")
    assert _service_extra_env(runner)[authority.GENERATION_ENV] == "0"
    assert "AVA_DB_URL" not in _service_extra_env(frontend)
    root = _root_child_env()
    assert "AVA_DB_URL" not in root and authority.GENERATION_ENV not in root


def test_bootstrap_projects_the_active_runner_generation(born: Born) -> None:
    from shared.config.service_read import _generation_runner_url

    name, password = born.login("runner")
    assert _generation_runner_url(born.endpoint(), born.home) == url_with_userinfo(
        born.endpoint(), name, password
    )


def test_revoked_generation_login_is_not_resurrected_by_start(born: Born) -> None:
    """A restored catalog that re-enables an old number is swept, never admitted."""
    with born.admin() as conn:
        conn.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD 'restored-old-login' IN ROLE ava_gateway"
            ).format(sql.Identifier("ava_g5_gateway"))
        )
    data_plane.complete_gateway_data_plane()
    _refused(
        host="127.0.0.1",
        port=born.pg_port,
        user="ava_g5_gateway",
        password="restored-old-login",  # noqa: S106 — the restored login's fixture credential
        dbname="ava",
    )


_ROLES = frozenset({"gateway", "agent-runner"})


def _collector_postgres_receiver(born: Born, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The rendered collector config's PostgreSQL receiver; the whole rendered
    text carries no credential of the data plane."""
    import yaml

    from cli.commands import _otel_collector as oc

    monkeypatch.setattr(settings.observability, "telemetry_otlp_enabled", True)
    monkeypatch.setattr("shared.machine.machine_name", lambda: "test-machine")
    rendered = oc.generate_config(_REPO, born.home, _ROLES)
    secret = authority.read_secret(born.home, authority.active_generation(born.home))
    credentials = {
        "gateway generation": secret.roles.gateway.password,
        "runner generation": secret.roles.runner.password,
        "pooler admin": authority.read_pooler_admin(born.home).password,
    }
    leaked = [name for name, value in credentials.items() if value in rendered]
    assert leaked == [], f"collector config carries credentials: {leaked}"
    return yaml.safe_load(rendered)["receivers"]["postgresql"]


def _dial_as_receiver(receiver: dict[str, Any], database: str) -> psycopg.Connection[Any]:
    """Dial exactly as otelcol-contrib's postgresql receiver builds its lib/pq
    DSN: `host:port` endpoint, `/` prefixed for the unix transport."""
    host, _, port = receiver["endpoint"].rpartition(":")
    if receiver["transport"] == "unix":
        host = "/" + host.lstrip("/")
    return psycopg.connect(
        host=host,
        port=port,
        user=receiver["username"],
        password=receiver["password"],
        dbname=database,
        sslmode="disable",
        connect_timeout=5,
        autocommit=True,
    )


def _scrape_like_the_receiver(receiver: dict[str, Any]) -> None:
    """The receiver's cluster-wide queries (on `postgres`) and per-database ones."""
    with _dial_as_receiver(receiver, "postgres") as conn:
        # Other sessions' activity/replication rows need the stats role.
        assert conn.execute("SELECT pg_has_role('pg_read_all_stats', 'USAGE')").fetchone() == (
            True,
        )
        sizes = conn.execute(
            "SELECT datname, pg_database_size(datname) FROM pg_catalog.pg_database"
            " WHERE datistemplate = false"
        ).fetchall()
        assert {name for name, _size in sizes} >= {"postgres", "ava"}
        conn.execute("SELECT datname, count(*) FROM pg_stat_activity GROUP BY datname")
        conn.execute(
            "SELECT coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn), -1)"
            " FROM pg_stat_replication"
        )
        conn.execute("SELECT coalesce(last_archived_time, CURRENT_TIMESTAMP) FROM pg_stat_archiver")
        conn.execute("SHOW max_connections")
    for database in receiver["databases"]:
        with _dial_as_receiver(receiver, database) as conn:
            tables = conn.execute(
                "SELECT relname, pg_relation_size(relid) FROM pg_stat_user_tables"
            ).fetchall()
            assert "agents" in {name for name, _size in tables}
            conn.execute("SELECT * FROM pg_statio_user_tables")
            conn.execute("SELECT pg_relation_size(indexrelid) FROM pg_stat_user_indexes")


def test_collector_postgres_receiver_keeps_no_credential_and_survives_rollover(
    born: Born, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collector's PostgreSQL receiver logs in as the stable monitoring
    role by `peer` over the owner-only socket: no password at rest in its
    config, no application data, and a write-generation rollover neither
    breaks nor closes it."""
    from uuid import uuid4

    receiver = _collector_postgres_receiver(born, monkeypatch)
    assert receiver["username"] == authority.MONITOR_ROLE
    assert receiver["transport"] == "unix"
    assert receiver["databases"] == ["ava"]
    _scrape_like_the_receiver(receiver)
    with _dial_as_receiver(receiver, "ava") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT id FROM agents LIMIT 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO agents (id) VALUES (920001)")
    # No password exists for the role: TCP (SCRAM) never admits it.
    _refused(
        host="127.0.0.1",
        port=born.pg_port,
        user=authority.MONITOR_ROLE,
        password=receiver["password"],
        dbname="ava",
    )

    old_gateway = born.login("gateway")
    rollout = authority.OperationAuthority(operation=uuid4(), direction="candidate")
    with _dial_as_receiver(receiver, "ava") as scraping, born.admin() as conn:
        authority.revoke(conn, born.home, rollout)
        authority.close_revoked(conn, born.home, rollout)
        verified = authority.mint_generation(conn, born.home, rollout)
        authority.activate(born.home, rollout, verified)
        # The fence closed the old generation, not the monitoring session.
        assert scraping.execute("SELECT session_user").fetchone() == (authority.MONITOR_ROLE,)
        authority.check_invariant(
            conn, born.home, database="ava", readonly_grantees=data_plane.READONLY_GRANTEES
        )
    assert authority.active_generation(born.home).number == 1
    _refused(
        host="127.0.0.1",
        port=born.pg_port,
        user=old_gateway[0],
        password=old_gateway[1],
        dbname="ava",
    )
    # The unchanged config keeps scraping under the new generation.
    assert _collector_postgres_receiver(born, monkeypatch) == receiver
    _scrape_like_the_receiver(receiver)


def test_direct_exemption_dials_postgres_as_the_delivered_login(
    born: Born, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With pooling on, the admin-plane `direct=True` dial swaps only the port to
    the real Postgres (the registry record) and keeps the delivered login."""
    import shared.db

    def _registry() -> dict[str, cluster.ClusterRecord]:
        return {str(born.home): born.record}

    monkeypatch.setattr(cluster, "load_registry", _registry)
    direct = shared.db.direct_db_url()
    assert direct == url_with_userinfo(
        born.endpoint().replace(str(born.pooler_port), str(born.pg_port)), *born.login("gateway")
    )
    with shared.db.connect(direct=True) as conn:
        assert conn.execute("SELECT inet_server_port(), session_user").fetchone() == (
            born.pg_port,
            born.login("gateway")[0],
        )
    with shared.db.connect() as conn:  # pooled: the one access URL
        assert conn.execute("SELECT session_user").fetchone() == (born.login("gateway")[0],)
