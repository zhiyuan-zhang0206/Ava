"""Current baseline schema and durable trigger contracts."""

from __future__ import annotations

import os
import time
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

from base.cluster.machine import MachineIdentity
from base.config import settings
from base.deploy.schema.migrations import (
    MigrationFailed,
    apply_pending_migrations,
    required_migration_set,
)
from base.deploy.schema.tests.migration_support import (
    _SCHEMA_SQL,
    _schema_sql_stamped_migration_names,
    _throwaway_database,
)
from base.deploy.schema.tests.migration_support import (
    _reset_schema_migrations_state as _reset_schema_migrations_state,
)

_MIGRATIONS = _SCHEMA_SQL.parents[1] / "migrations"


def test_fresh_schema_sql_bootstrap_is_baselined(machine_identity: MachineIdentity) -> None:
    """The real fresh-DB bootstrap: apply db/schema.sql to an empty database and
    assert it lands in the baselined applied-set state (new shape + baseline row,
    presets seeded), and apply_pending is then a no-op. Isolated throwaway DB, so
    this never touches the shared session DB."""
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_mig_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    schema = _SCHEMA_SQL.read_text()

    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(schema)  # type: ignore[arg-type]  # trusted multi-statement schema
            expected_stamped = {(name,) for name in _schema_sql_stamped_migration_names()}
            row = conn.execute("SELECT name FROM schema_migrations").fetchall()
            assert set(row) == expected_stamped
            presets = conn.execute("SELECT name FROM agent_presets").fetchall()
            assert {r[0] for r in presets} == {
                "coder",
                "reviewer",
                "researcher",
                "orchestrator",
                "explorer",
            }
            default_model = conn.execute(
                "SELECT llm_model FROM cluster_defaults WHERE id = 1"
            ).fetchone()
            assert default_model == ("deepseek-flash",)
        # Apply on the baselined DB: the folded migration marker makes the strict
        # ALTER skip a fresh schema that already carries the column; all other
        # post-baseline migrations replay cleanly, then a second apply is a no-op.
        with psycopg.connect(url) as conn:
            assert set(
                apply_pending_migrations(conn, machine_name=machine_identity.name)
            ) == required_migration_set() - set(_schema_sql_stamped_migration_names())
        with psycopg.connect(url) as conn:
            assert apply_pending_migrations(conn, machine_name=machine_identity.name) == []
            settled = conn.execute("SELECT llm_model FROM cluster_defaults WHERE id = 1").fetchone()
            assert settled == ("deepseek-flash",)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def test_lifecycle_pointer_done_guard_blocks_torn_commit_and_allows_settle() -> None:
    """The commit-time fence rejects pointer -> done and passes the same-transaction settle.

    Every legitimate writer clears the pointer in the same transaction, so the
    deferred fence is invisible to them; only a torn write — e.g. a manual
    "cleanup" UPDATE that flips a command to done without settling it — crosses
    a commit and fails.
    """
    with _throwaway_database("lifecycle_guard") as url:
        with psycopg.connect(url, autocommit=True) as setup, setup.cursor() as cur:
            cur.execute(sql.SQL(cast(LiteralString, _SCHEMA_SQL.read_text())), prepare=False)
            cur.execute("INSERT INTO agents (id) SELECT generate_series(1, 2)")
            cur.execute(
                "INSERT INTO agents_meta (id, status) VALUES (1, 'terminated'), (2, 'terminated')"
            )
            claimed: list[int] = []
            for agent_id in (1, 2):
                cur.execute(
                    "INSERT INTO inbound_messages "
                    "(agent_id, content, kind, source, status, applied_at, claimed_at, "
                    " target_generation, target_owner) "
                    "VALUES (%s, '', 'terminate', 'user', 'claimed', now(), now(), "
                    "gen_random_uuid(), gen_random_uuid()) RETURNING id",
                    (agent_id,),
                )
                row = cur.fetchone()
                assert row is not None
                claimed.append(row[0])
                cur.execute(
                    "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s",
                    (row[0], agent_id),
                )

        # Positive: the documented settle shape — done + pointer cleared in ONE
        # transaction — commits cleanly under the deferred guard.
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (claimed[0],))
            cur.execute("UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=1")
        # (`with` exit committed.)

        # Negative: the pointer left alive across the commit — rejected at commit.
        conn = psycopg.connect(url)
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (claimed[1],))
            with pytest.raises(psycopg.errors.RaiseException, match="cannot reach done"):
                conn.commit()
            conn.rollback()
        finally:
            conn.close()


def test_schema_sql_has_birth_config_column() -> None:
    """Terminal-state pin (the pre-reset birth-config backfill contract, now
    folded into the baseline): agents_meta carries birth_config, and the
    CHECK-level comment in schema.sql documents the overlay-vs-default
    precedence the migration used to enforce by hand."""
    schema = _SCHEMA_SQL.read_text()
    assert "birth_config               JSONB" in schema, (
        "birth_config column missing from agents_meta"
    )


def test_schema_sql_has_r1_deploy_state_tables() -> None:
    """Terminal-state pin (the pre-reset r1 deploy-state backfill contract):
    the baseline carries both deploy-state tables the rollout machinery reads."""
    schema = _SCHEMA_SQL.read_text()
    for table in ("host_deploy_state",):
        assert f"CREATE TABLE {table}" in schema, f"{table} missing from baseline"


def test_schema_sql_seeds_presets_without_a_skill_index() -> None:
    """The baseline must agree with the migrated state: a fresh DB's seed presets
    carry no skills_to_inject_into_system_prompt, so fresh and upgraded clusters
    do not disagree about what a `coder` is."""
    seed_block = _SCHEMA_SQL.read_text().split("INSERT INTO agent_presets")[1]
    seed_block = seed_block.split("ON CONFLICT")[0]
    assert "skills_to_inject_into_system_prompt" not in seed_block


# ── Contraction of the retired deploy and watcher storage ──
#
# `20261001T055030_drop-retired-deploy-and-watcher-storage` drops the objects whose
# readers and writers are gone, and `20261001T055130_retire-restarting-and-publication-deferred`
# tightens two `agents_meta` CHECKs; the retirement refuses a stuck row.

_DROP = "20261001T055030_drop-retired-deploy-and-watcher-storage"
_RETIRE = "20261001T055130_retire-restarting-and-publication-deferred"


def _widen_retired_checks(conn: psycopg.Connection) -> None:
    """The two `agents_meta` CHECKs as they stood before the retirement migration."""
    conn.execute("ALTER TABLE agents_meta DROP CONSTRAINT agents_meta_status_check")
    conn.execute(
        "ALTER TABLE agents_meta ADD CONSTRAINT agents_meta_status_check "
        "CHECK (status IN ('running', 'idling', 'restarting', 'terminated'))"
    )


def test_the_version_gate_and_posture_paths_survive_the_contraction(
    machine_identity: MachineIdentity,
) -> None:
    """Everything a live process still does against the two surviving tables."""
    with _throwaway_database("retire_paths") as url, psycopg.connect(url) as conn:
        conn.execute(cast(LiteralString, _SCHEMA_SQL.read_text()))
        conn.commit()
        apply_pending_migrations(conn, machine_name=machine_identity.name)
        conn.execute(
            "UPDATE deployment_state SET min_code_version = GREATEST(min_code_version, %s) "
            "WHERE id = 1",
            (2700,),
        )
        assert conn.execute(
            "SELECT COALESCE((SELECT min_code_version FROM public.deployment_state WHERE id = 1), 0)"
        ).fetchone() == (2700,)
        for posture in ("paused", "idle"):
            conn.execute(
                "INSERT INTO host_deploy_state (machine, posture, updated_at) "
                "VALUES ('m1', %s, now()) ON CONFLICT (machine) DO UPDATE "
                "SET posture = EXCLUDED.posture, updated_at = EXCLUDED.updated_at",
                (posture,),
            )
        assert conn.execute("SELECT posture FROM host_deploy_state").fetchone() == ("idle",)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE host_deploy_state SET posture = 'converging'")
        conn.rollback()
        conn.execute("INSERT INTO agents (id, label) VALUES (1, 'a')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO agents_meta (id, status, machine) VALUES (1, 'restarting', 'm')"
            )
        conn.rollback()
        conn.execute("INSERT INTO agents (id, label) VALUES (2, 'b')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO agents_meta (id, status, machine, last_admission_outcome, "
                "last_admission_at) VALUES (2, 'idling', 'm', 'publication_deferred', now())"
            )


def test_restarting_row_refuses_the_retirement_and_leaves_the_schema_alone(
    machine_identity: MachineIdentity,
) -> None:
    with _throwaway_database("retire_guard") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, _SCHEMA_SQL.read_text()))
            _widen_retired_checks(conn)
            conn.execute("INSERT INTO agents (id, label) VALUES (7, 'stuck'), (8, 'ok')")
            conn.execute(
                "INSERT INTO agents_meta (id, status, machine) VALUES "
                "(7, 'restarting', 'm'), (8, 'idling', 'm')"
            )

        with psycopg.connect(url) as conn, pytest.raises(MigrationFailed) as refusal:
            apply_pending_migrations(conn, machine_name=machine_identity.name)
        assert isinstance(refusal.value.__cause__, psycopg.errors.RaiseException)
        assert "rows 7 are still in it" in str(refusal.value.__cause__)

        with psycopg.connect(url, autocommit=True) as conn:
            names = {
                row[0] for row in conn.execute("SELECT name FROM schema_migrations").fetchall()
            }
            assert _RETIRE not in names
            assert conn.execute("SELECT status FROM agents_meta WHERE id = 7").fetchone() == (
                "restarting",
            )
            # The drop migration ran first and committed on its own; only the
            # retirement refused, so the status CHECK still admits the value.
            assert _DROP in names
            definition = conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'agents_meta_status_check'"
            ).fetchone()
            assert definition is not None and "restarting" in definition[0]

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("UPDATE agents_meta SET status = 'idling' WHERE id = 7")
        with psycopg.connect(url) as conn:
            # Migrations newer than the retirement apply after it; only the
            # retirement was held back by the refusal.
            applied = apply_pending_migrations(conn, machine_name=machine_identity.name)
            assert [name for name in applied if name <= _RETIRE] == [_RETIRE]
        with (
            psycopg.connect(url, autocommit=True) as conn,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            conn.execute("UPDATE agents_meta SET status = 'restarting' WHERE id = 8")


# ── Contraction of the delivery watchdog's durable retry state ──
#
# `20261011T191500_drop-dead-delivery-watchdog-state` drops the two watchdog
# tables and the three per-inbound dispatch columns nothing reads or writes any more.

_WATCHDOG_STATE = "20261011T191500_drop-dead-delivery-watchdog-state"
_WATCHDOG_TABLES = ("delivery_watchdog_alerted", "delivery_watchdog_attempts")
_DISPATCH_COLUMNS = ("dispatch_count", "last_dispatch_at", "poisoned_at")


def _watchdog_state_present(conn: psycopg.Connection) -> tuple[list[str], list[str]]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s) ORDER BY 1",
            (list(_WATCHDOG_TABLES),),
        ).fetchall()
    ]
    columns = [
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'inbound_messages' "
            "AND column_name = ANY(%s) ORDER BY 1",
            (list(_DISPATCH_COLUMNS),),
        ).fetchall()
    ]
    return tables, columns


def test_a_fresh_database_has_none_of_the_dead_watchdog_state(
    machine_identity: MachineIdentity,
) -> None:
    with _throwaway_database("watchdog_fresh") as url, psycopg.connect(url) as conn:
        conn.execute(cast(LiteralString, _SCHEMA_SQL.read_text()))
        conn.commit()
        assert _watchdog_state_present(conn) == ([], [])
        apply_pending_migrations(conn, machine_name=machine_identity.name)
        assert _watchdog_state_present(conn) == ([], [])


def test_the_contraction_drops_the_state_of_an_upgraded_cluster_and_keeps_its_rows(
    machine_identity: MachineIdentity,
) -> None:
    """An older cluster carries the objects (and rows in them); the migration removes
    them, keeps every other inbound column, and replays as a no-op."""
    with _throwaway_database("watchdog_upgrade") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, _SCHEMA_SQL.read_text()))
            conn.execute(
                "ALTER TABLE inbound_messages "
                "ADD COLUMN dispatch_count INT NOT NULL DEFAULT 0, "
                "ADD COLUMN last_dispatch_at TIMESTAMPTZ, "
                "ADD COLUMN poisoned_at TIMESTAMPTZ"
            )
            conn.execute(
                "CREATE TABLE delivery_watchdog_alerted ("
                "inbound_id BIGINT PRIMARY KEY REFERENCES inbound_messages(id) ON DELETE CASCADE, "
                "alerted_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            conn.execute(
                "CREATE TABLE delivery_watchdog_attempts ("
                "kind TEXT NOT NULL, agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE, "
                "last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), "
                "consecutive_failures INT NOT NULL DEFAULT 0, suppress_count INT NOT NULL DEFAULT 0, "
                "PRIMARY KEY (kind, agent_id))"
            )
            conn.execute("INSERT INTO agents (id, label) VALUES (1, 'a')")
            conn.execute("INSERT INTO agents_meta (id, status, machine) VALUES (1, 'idling', 'm')")
            conn.execute(
                "INSERT INTO inbound_messages (id, agent_id, kind, content, dispatch_count) "
                "VALUES (10, 1, 'chat', 'hello', 3)"
            )
            conn.execute("INSERT INTO delivery_watchdog_alerted (inbound_id) VALUES (10)")
            conn.execute(
                "INSERT INTO delivery_watchdog_attempts (kind, agent_id) VALUES ('resurrect', 1)"
            )
            assert _watchdog_state_present(conn) == (
                list(_WATCHDOG_TABLES),
                sorted(_DISPATCH_COLUMNS),
            )
            conn.execute("DELETE FROM schema_migrations WHERE name = %s", (_WATCHDOG_STATE,))

        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(conn, machine_name=machine_identity.name)
            assert _WATCHDOG_STATE in applied
            assert _watchdog_state_present(conn) == ([], [])
            kept = conn.execute("SELECT content, status FROM inbound_messages WHERE id = 10")
            assert kept.fetchone() == ("hello", "pending")

        with psycopg.connect(url) as conn:
            assert apply_pending_migrations(conn, machine_name=machine_identity.name) == []
            migration = next(
                path for path in _MIGRATIONS.rglob(f"{_WATCHDOG_STATE}.sql")
            ).read_text()
            conn.execute(cast(LiteralString, migration))  # replay: every statement is IF EXISTS
            assert _watchdog_state_present(conn) == ([], [])
