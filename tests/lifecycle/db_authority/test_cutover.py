"""The one-time database cutover of a legacy home, on real PostgreSQL + PgBouncer.

A legacy home is reproduced as the previous bring-up left it: trust `pg_hba`
lines, a LOGIN schema owner with `AVA_DB_ADMIN_PASSWORD`, a LOGIN `ava_runner`
with `AVA_RUNNER_DB_PASSWORD`, an owner-URL `.env`, a trust pooler, and a
legacy session held open. `scripts/cutover_db_authority.py`'s db step must
convert it (NOLOGIN owner and groups, ledger, generation 0, SCRAM pooler,
credential-free `.env`), close every legacy session, stay idempotent, resume a
crash with the same generation, and refuse ambiguous state before any effect.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg import sql

from cli.commands import _cluster_instance as ci
from cli.commands import _data_plane as data_plane
from cli.commands import _pgbouncer as pooler
from cli.commands.migrations import cmd_migrations_apply
from scripts import cutover_db_authority as cutover
from shared import cluster
from shared.cluster import authority, ownership
from shared.config import settings
from shared.pg_admin import owner_session
from tests._containers import _free_port

pytestmark = pytest.mark.skipif(
    not (Path(pooler.pgbouncer_bin()).exists() or shutil.which(pooler.pgbouncer_bin())),
    reason="pgbouncer not installed (brew/apt)",
)

_OWNER_PW = "legacy-owner-password"
_RUNNER_PW = "legacy-runner-password"
_BEARER = "legacy-bearer"
_LEGACY_HBA = "local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\n"


@dataclass(frozen=True)
class Legacy:
    home: Path
    record: cluster.ClusterRecord

    @property
    def pg_port(self) -> int:
        return self.record.ports["postgres"]

    @property
    def pooler_port(self) -> int:
        return cluster.record_pgbouncer_port(self.record)

    def admin(self) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host=str(ci._pg_socket_dir()), port=self.pg_port, dbname="ava", autocommit=True
        )


def _reload(port: int) -> None:
    with psycopg.connect(ci.pg_admin_url(port), autocommit=True) as conn:
        conn.execute("SELECT pg_reload_conf()")


def _legacy_pooler(home: Path, pg_port: int, listen_port: int) -> None:
    """The previous trust pooler, under this home's pooler custody paths."""
    directory = home / "pgbouncer"
    directory.mkdir(mode=0o700, exist_ok=True)
    (directory / "userlist.txt").write_text(f'"ava" "{_OWNER_PW}"\n')
    (directory / "pgbouncer.ini").write_text(
        "[databases]\n"
        f"ava = host={ci._pg_socket_dir()} port={pg_port} dbname=ava\n"
        "[pgbouncer]\n"
        f"listen_addr = 127.0.0.1\nlisten_port = {listen_port}\nauth_type = trust\n"
        f"auth_file = {directory / 'userlist.txt'}\npool_mode = transaction\n"
        "ignore_startup_parameters = extra_float_digits,options\n"
        f"admin_users = ava\nlogfile = {directory / 'pgbouncer.log'}\n"
        f"pidfile = {directory / 'pgbouncer.pid'}\n"
    )
    subprocess.run(  # noqa: S603 — resolved pgbouncer binary, private fixture config
        [pooler.pgbouncer_bin(), "-d", str(directory / "pgbouncer.ini")],
        check=True,
        capture_output=True,
        timeout=10,
    )
    ci._wait_for_reachable_bind()


def _provision_legacy(port: int) -> None:
    admin = ci.pg_admin_url(port)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE ava LOGIN NOSUPERUSER PASSWORD {}").format(sql.Literal(_OWNER_PW))
        )
        conn.execute("CREATE DATABASE ava OWNER ava")
    schema = (Path(__file__).resolve().parents[3] / "db" / "schema.sql").read_text()
    with owner_session(admin, database="ava", owner="ava", autocommit=True) as conn:
        conn.execute(schema)  # type: ignore[arg-type]
    cluster.ensure_checkpoint_schema("ava", base_admin_url=admin, database_created=True)
    cmd_migrations_apply()
    with psycopg.connect(
        ci.pg_admin_url(port).replace("/postgres", "/ava"), autocommit=True
    ) as conn:
        # The historical runner login and a slice of its old point-in-time grants.
        conn.execute(
            sql.SQL("CREATE ROLE ava_runner LOGIN NOSUPERUSER PASSWORD {}").format(
                sql.Literal(_RUNNER_PW)
            )
        )
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO ava_runner")


def _legacy_env(home: Path, pooler_port: int) -> None:
    lines = {
        "AVA_DB_URL": f"postgresql://ava:{_OWNER_PW}@127.0.0.1:{pooler_port}/ava",
        "AVA_DB_ADMIN_PASSWORD": _OWNER_PW,
        "AVA_RUNNER_DB_PASSWORD": _RUNNER_PW,
        "AVA_CLUSTER_SECRET": _BEARER,
    }
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in lines.items()))


@pytest.fixture
def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Legacy]:
    home = (tmp_path / "home").resolve()
    home.mkdir(mode=0o700)
    monkeypatch.setattr(settings.general, "ava_home", str(home))
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))
    ports = dict(cluster.LEGACY_AVA_PORTS)
    ports.update(postgres=_free_port(), redis=_free_port(), pgbouncer=_free_port())
    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", ports), gateway_home=str(home), created_at="test"
    )

    def _record(_home: Path) -> cluster.ClusterRecord:
        return record

    monkeypatch.setattr(cluster, "get_record", _record)
    dp = settings.data_plane
    monkeypatch.setattr(dp, "cluster_secret", _BEARER)
    monkeypatch.setattr(dp, "pgbouncer_enabled", True)
    pooler_port = cluster.record_pgbouncer_port(record)
    monkeypatch.setattr(dp, "db_url", f"postgresql://ava:{_OWNER_PW}@127.0.0.1:{pooler_port}/ava")
    legacy = Legacy(home=home, record=record)
    try:
        assert ci._start_pg(legacy.pg_port, _BEARER) == 0
        (home / "pg" / "pg_hba.conf").write_text(_LEGACY_HBA)
        _reload(legacy.pg_port)
        _provision_legacy(legacy.pg_port)
        _legacy_env(home, pooler_port)
        _legacy_pooler(home, legacy.pg_port, pooler_port)
        yield legacy
    finally:
        monkeypatch.setattr(settings.general, "ava_home", str(home))
        pooler.stop_pgbouncer(force=True)
        subprocess.run(  # noqa: S603 — resolved pg_ctl + test-owned data dir
            [ci._pg_bin("pg_ctl"), "-D", str(home / "pg"), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )


def _role(legacy: Legacy, name: str) -> tuple[bool, bool] | None:
    with legacy.admin() as conn:
        row = conn.execute(
            "SELECT rolcanlogin, rolpassword IS NULL FROM pg_authid WHERE rolname = %s", (name,)
        ).fetchone()
    return None if row is None else (bool(row[0]), bool(row[1]))


def _refused(**kwargs: Any) -> None:
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(connect_timeout=5, **kwargs).close()


def test_dry_run_reports_the_conversion_and_changes_nothing(legacy: Legacy) -> None:
    before = (legacy.home / ".env").read_bytes()
    outcome = cutover.convert_db(legacy.home, legacy.record, execute=False)
    assert outcome.startswith("db: would convert")
    assert (legacy.home / ".env").read_bytes() == before
    assert authority.load_ledger(legacy.home) is None
    assert _role(legacy, "ava") == (True, False)


def test_converts_a_legacy_home_and_closes_every_legacy_session(legacy: Legacy) -> None:
    held = psycopg.connect(
        host="127.0.0.1", port=legacy.pg_port, user="ava", password=_OWNER_PW, dbname="ava"
    )
    held.execute("SELECT 1")
    held.commit()

    outcome = cutover.convert_db(legacy.home, legacy.record, execute=True)

    assert "converted, write generation 0 active" in outcome
    assert cutover.read_journal(legacy.home) == {"db": "done"}
    ledger = authority.require_ledger(legacy.home)
    assert ledger.active is not None and ledger.active.origin.kind == "cutover"
    # Owner and historical runner demoted in place; the gateway group is new.
    assert _role(legacy, "ava") == (False, True)
    assert _role(legacy, "ava_runner") == (False, True)
    assert _role(legacy, "ava_gateway") == (False, True)
    # The legacy session was terminated by the closure proof.
    with pytest.raises(psycopg.OperationalError):
        held.execute("SELECT 1")
    held.close()
    # No legacy credential opens a door; the generation's logins do.
    _refused(host="127.0.0.1", port=legacy.pg_port, user="ava", password=_OWNER_PW, dbname="ava")
    _refused(
        host="127.0.0.1", port=legacy.pg_port, user="ava_runner", password=_RUNNER_PW, dbname="ava"
    )
    _refused(
        host="127.0.0.1", port=legacy.pooler_port, user="ava", password=_OWNER_PW, dbname="ava"
    )
    secret = authority.read_secret(legacy.home, ledger.active)
    for role in (secret.roles.gateway, secret.roles.runner):
        with psycopg.connect(
            host="127.0.0.1",
            port=legacy.pooler_port,
            user=role.name,
            password=role.password,
            dbname="ava",
            prepare_threshold=None,
        ) as conn:
            assert conn.execute("SELECT count(*) FROM agents").fetchone() is not None
    # `.env` holds only the credential-free endpoint.
    env = dotenv_values(legacy.home / ".env")
    assert env["AVA_DB_URL"] == f"postgresql://ava@127.0.0.1:{legacy.pooler_port}/ava"
    assert "AVA_DB_ADMIN_PASSWORD" not in env and "AVA_RUNNER_DB_PASSWORD" not in env
    assert env["AVA_CLUSTER_SECRET"] == _BEARER
    with legacy.admin() as conn:
        authority.check_invariant(
            conn, legacy.home, database="ava", readonly_grantees=data_plane.READONLY_GRANTEES
        )


def test_repeat_is_a_verified_no_op(legacy: Legacy) -> None:
    cutover.convert_db(legacy.home, legacy.record, execute=True)
    paths = (
        legacy.home / ".env",
        legacy.home / "db-authority" / "ledger.json",
        cutover.journal_path(legacy.home),
    )
    before = [path.read_bytes() for path in paths]
    owner = ownership.pooler(pooler._ini_path(), pooler._pidfile_path())
    assert "verified, write generation 0 active" in cutover.convert_db(
        legacy.home, legacy.record, execute=True
    )
    assert [path.read_bytes() for path in paths] == before
    after = ownership.pooler(pooler._ini_path(), pooler._pidfile_path())
    assert owner is not None and after is not None and owner.pid == after.pid


def test_a_crash_resumes_with_the_same_generation(
    legacy: Legacy, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = data_plane.prove_generation_logins

    def crash(home: Path, generation: authority.Generation, endpoint: str) -> None:
        raise RuntimeError("injected crash before activation")

    monkeypatch.setattr(data_plane, "prove_generation_logins", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        cutover.convert_db(legacy.home, legacy.record, execute=True)
    assert cutover.read_journal(legacy.home) == {"db": "converting"}
    ledger = authority.require_ledger(legacy.home)
    assert ledger.active is None and ledger.pending is not None
    pending = ledger.pending
    # The legacy `.env` is rewritten only after activation and the invariant.
    assert "AVA_DB_ADMIN_PASSWORD" in dotenv_values(legacy.home / ".env")

    monkeypatch.setattr(data_plane, "prove_generation_logins", real)
    assert "converted" in cutover.convert_db(legacy.home, legacy.record, execute=True)
    ledger = authority.require_ledger(legacy.home)
    assert ledger.active is not None and ledger.counter == 0
    assert ledger.active.credential_digest == pending.credential_digest
    assert cutover.read_journal(legacy.home) == {"db": "done"}


def test_ambiguous_state_is_refused_before_any_effect(legacy: Legacy) -> None:
    # A journal that claims completion while `.env` still holds legacy keys.
    cutover._record(legacy.home, "db", "done")
    before = (legacy.home / ".env").read_bytes()
    with pytest.raises(cutover.CutoverRefusedError, match="legacy credentials"):
        cutover.convert_db(legacy.home, legacy.record, execute=True)
    assert (legacy.home / ".env").read_bytes() == before
    assert _role(legacy, "ava") == (True, False)


def test_a_converted_home_whose_env_regained_a_legacy_key_is_refused(legacy: Legacy) -> None:
    cutover.convert_db(legacy.home, legacy.record, execute=True)
    with (legacy.home / ".env").open("a") as env:
        env.write(f"AVA_DB_ADMIN_PASSWORD={_OWNER_PW}\n")
    with pytest.raises(cutover.CutoverRefusedError, match="legacy credentials"):
        cutover.convert_db(legacy.home, legacy.record, execute=True)


def test_an_env_naming_another_owner_than_its_database_is_refused(legacy: Legacy) -> None:
    env = (legacy.home / ".env").read_text().replace("postgresql://ava:", "postgresql://ava_x:")
    (legacy.home / ".env").write_text(env)
    with pytest.raises(cutover.CutoverRefusedError, match="same-named database"):
        cutover.convert_db(legacy.home, legacy.record, execute=True)
    assert not cutover.journal_path(legacy.home).exists()


def test_the_journal_schema_accepts_only_known_db_states(legacy: Legacy) -> None:
    path = cutover.journal_path(legacy.home)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "home": str(legacy.home), "steps": {"db": "x"}}))
    with pytest.raises(cutover.CutoverRefusedError, match="unrecognized cutover journal step"):
        cutover.read_journal(legacy.home)


def test_a_superuser_owner_is_refused_before_any_effect(legacy: Legacy) -> None:
    with legacy.admin() as conn:
        conn.execute("ALTER ROLE ava SUPERUSER")
    pooler_before = ownership.pooler(pooler._ini_path(), pooler._pidfile_path())
    with pytest.raises(cutover.CutoverRefusedError, match="superuser"):
        cutover.convert_db(legacy.home, legacy.record, execute=True)
    assert not cutover.journal_path(legacy.home).exists()
    pooler_after = ownership.pooler(pooler._ini_path(), pooler._pidfile_path())
    assert pooler_before is not None and pooler_after is not None
    assert pooler_before.pid == pooler_after.pid
    with legacy.admin() as conn:
        conn.execute("ALTER ROLE ava NOSUPERUSER")
