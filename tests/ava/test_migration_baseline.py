"""Current baseline schema and durable trigger contracts."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

from base.cluster.dataplane.pg_tools import pg_tool
from base.config import settings
from base.deploy.schema.migrations import (
    MigrationFailed,
    apply_pending_migrations,
    required_migration_set,
)
from tests.ava.migration_support import (
    _SCHEMA_SQL,
    _schema_sql_stamped_migration_names,
    _throwaway_database,
)
from tests.ava.migration_support import (
    _reset_schema_migrations_state as _reset_schema_migrations_state,
)


def test_fresh_schema_sql_bootstrap_is_baselined() -> None:
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
            assert set(apply_pending_migrations(conn)) == required_migration_set() - set(
                _schema_sql_stamped_migration_names()
            )
        with psycopg.connect(url) as conn:
            assert apply_pending_migrations(conn) == []
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
# tightens two `agents_meta` CHECKs. Both must apply cleanly to a database that still
# holds the objects (with data), land on exactly `db/schema.sql`, and roll back to the
# pre-migration shape, which their `.down.sql` files rebuild from the baseline in the
# reverse order a manual rollback uses.

_DIRECTORY = Path(__file__).resolve().parents[2] / "migrations"
_DROP = "20261001T055030_drop-retired-deploy-and-watcher-storage"
_RETIRE = "20261001T055130_retire-restarting-and-publication-deferred"

_RETIRED_DEPLOYMENT_COLUMNS = {
    "phase",
    "kind",
    "holder",
    "acquired_at",
    "expires_at",
    "settle_hosts",
    "settle_note",
    "settle_started_at",
    "outcome",
    "failing_step",
    "started_at",
    "ended_at",
    "origin",
    "target_sha",
    "observed_by",
    "log_path",
    "pin_advanced",
    "managed_writer_evidence",
}


def _sql(name: str, *, down: bool = False) -> LiteralString:
    suffix = ".down.sql" if down else ".sql"
    return cast(LiteralString, (_DIRECTORY / f"{name}{suffix}").read_text())


def _schema_dump(url: str) -> str:
    result = subprocess.run(  # noqa: S603 — fixed tool path against a private throwaway database
        [
            str(pg_tool("pg_dump")),
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return "\n".join(
        line
        for line in result.stdout.splitlines()
        if not line.startswith(("\\restrict ", "\\unrestrict "))
    )


def _order_insensitive(dump: str) -> list[str]:
    """The dump as a sorted list of object blocks with each table's lines sorted.

    A rollback re-adds a dropped column at the end of its table, so the restored
    table has the same columns in a different order; every reader names its
    columns. Nothing else about the shape may differ.
    """
    blocks: list[str] = []
    for block in re.split(r"\n(?=--\n-- Name: )", dump):
        table = re.search(r"CREATE TABLE [^\n]*\(\n(.*?)\n\);", block, re.DOTALL)
        if table is None:
            blocks.append(block)
            continue
        lines = sorted(line.rstrip(",") for line in table.group(1).splitlines())
        blocks.append(block[: table.start(1)] + "\n".join(lines) + block[table.end(1) :])
    return sorted(blocks)


def _columns(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {row[0] for row in rows}


def _tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    return {row[0] for row in rows}


def _roll_back_both(conn: psycopg.Connection) -> None:
    conn.execute(_sql(_RETIRE, down=True), prepare=False)
    conn.execute(_sql(_DROP, down=True), prepare=False)


def _seed_retired_state(conn: psycopg.Connection) -> None:
    """Production-like content in the objects the migrations drop."""
    conn.execute(
        "UPDATE deployment_state SET phase = 'stable', outcome = 'clean', "
        "target_sha = 'c2df669', log_path = '/logs/rollout.log', pin_advanced = TRUE, "
        "min_code_version = 2694 WHERE id = 1"
    )
    conn.execute("UPDATE cluster_pin SET target_sha = 'c2df669', last_known_good_sha = 'c2df669'")
    conn.execute("UPDATE cluster_last_update SET outcome = 'clean', target_sha = 'c2df669'")
    conn.execute(
        "INSERT INTO host_deploy_state (machine, posture, paused_at, stranded_hold_attempts) "
        "VALUES ('m-idle', 'idle', now(), 0), ('m-paused', 'paused', now(), 0), "
        "('m-converging', 'converging', now(), 0)"
    )
    conn.execute(
        "INSERT INTO agents (id, label) VALUES (1, 'closed'), (2, 'deferred'), (3, 'kept')"
    )
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine, closed_at, last_admission_outcome, "
        "last_admission_at) VALUES "
        "(1, 'terminated', 'm', now(), NULL, NULL), "
        "(2, 'idling', 'm', NULL, 'publication_deferred', now()), "
        "(3, 'idling', 'm', NULL, 'admitted', now())"
    )
    conn.execute(
        "INSERT INTO agent_watchers (session_id, agent_id, kind, name, status) "
        "VALUES (1, 3, 'at', 'w', 'reaped')"
    )


def _assert_retired_objects_gone_and_data_kept(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        assert _columns(conn, "deployment_state") == {"id", "min_code_version"}
        assert conn.execute("SELECT id, min_code_version FROM deployment_state").fetchall() == [
            (1, 2694)
        ]
        assert {"agent_watchers", "cluster_pin", "cluster_last_update"}.isdisjoint(_tables(conn))
        assert conn.execute(
            "SELECT to_regprocedure('public.lock_runtime_publication_admission()')"
        ).fetchone() == (None,)
        assert _columns(conn, "host_deploy_state") == {"machine", "posture", "updated_at"}
        assert conn.execute(
            "SELECT machine, posture FROM host_deploy_state ORDER BY machine"
        ).fetchall() == [("m-converging", "idle"), ("m-idle", "idle"), ("m-paused", "paused")]
        assert "closed_at" not in _columns(conn, "agents_meta")
        assert conn.execute(
            "SELECT id, last_admission_outcome, last_admission_at IS NULL "
            "FROM agents_meta WHERE id IN (2, 3) ORDER BY id"
        ).fetchall() == [(2, None, True), (3, "admitted", False)]
        names = {row[0] for row in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        assert {_DROP, _RETIRE} <= names


def test_up_lands_on_the_baseline_and_drops_only_the_retired_objects() -> None:
    with _throwaway_database("retire_up") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, _SCHEMA_SQL.read_text()))
        baseline = _schema_dump(url)
        with psycopg.connect(url, autocommit=True) as conn:
            _roll_back_both(conn)
            _seed_retired_state(conn)
            assert _columns(conn, "deployment_state") > _RETIRED_DEPLOYMENT_COLUMNS
            assert {"agent_watchers", "cluster_pin", "cluster_last_update"} <= _tables(conn)
        assert _schema_dump(url) != baseline

        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(conn)
        assert [name for name in applied if name in (_DROP, _RETIRE)] == [_DROP, _RETIRE]

        assert _schema_dump(url) == baseline
        _assert_retired_objects_gone_and_data_kept(url)


def test_the_version_gate_and_posture_paths_survive_the_contraction() -> None:
    """Everything a live process still does against the two surviving tables."""
    with _throwaway_database("retire_paths") as url, psycopg.connect(url) as conn:
        conn.execute(cast(LiteralString, _SCHEMA_SQL.read_text()))
        conn.commit()
        apply_pending_migrations(conn)
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


def test_down_restores_the_shape_and_up_again_lands_on_the_baseline() -> None:
    with _throwaway_database("retire_down") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, _SCHEMA_SQL.read_text()))
        baseline = _schema_dump(url)

        with psycopg.connect(url, autocommit=True) as conn:
            _roll_back_both(conn)
            assert _columns(conn, "deployment_state") > _RETIRED_DEPLOYMENT_COLUMNS
            assert conn.execute(
                "SELECT phase, kind, pin_advanced, min_code_version FROM deployment_state"
            ).fetchall() == [("stable", None, False, 0)]
            assert conn.execute("SELECT count(*) FROM cluster_pin").fetchone() == (1,)
            assert conn.execute("SELECT count(*) FROM cluster_last_update").fetchone() == (1,)
            assert conn.execute(
                "SELECT to_regprocedure('public.lock_runtime_publication_admission()') IS NOT NULL"
            ).fetchone() == (True,)
            conn.execute(
                "INSERT INTO host_deploy_state (machine, posture) VALUES ('m', 'converging')"
            )
            conn.execute("DELETE FROM host_deploy_state")
            conn.execute("INSERT INTO agents (id, label) VALUES (1, 'a')")
            conn.execute(
                "INSERT INTO agents_meta (id, status, machine) VALUES (1, 'restarting', 'm')"
            )
            conn.execute("UPDATE agents_meta SET status = 'idling' WHERE id = 1")
            conn.execute(
                "UPDATE agents_meta SET last_admission_outcome = 'publication_deferred', "
                "last_admission_at = now() WHERE id = 1"
            )
            conn.execute(
                "UPDATE agents_meta SET last_admission_outcome = NULL, last_admission_at = NULL"
            )
        rolled_back = _schema_dump(url)

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(_sql(_DROP), prepare=False)
            conn.execute(_sql(_RETIRE), prepare=False)
        assert _schema_dump(url) == baseline

        with psycopg.connect(url, autocommit=True) as conn:
            _roll_back_both(conn)
        assert _order_insensitive(_schema_dump(url)) == _order_insensitive(rolled_back)
        assert _order_insensitive(rolled_back) != _order_insensitive(baseline)


def test_restarting_row_refuses_the_retirement_and_leaves_the_schema_alone() -> None:
    with _throwaway_database("retire_guard") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, _SCHEMA_SQL.read_text()))
            _roll_back_both(conn)
            conn.execute("INSERT INTO agents (id, label) VALUES (7, 'stuck'), (8, 'ok')")
            conn.execute(
                "INSERT INTO agents_meta (id, status, machine) VALUES "
                "(7, 'restarting', 'm'), (8, 'idling', 'm')"
            )

        with psycopg.connect(url) as conn, pytest.raises(MigrationFailed) as refusal:
            apply_pending_migrations(conn)
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
            assert apply_pending_migrations(conn) == [_RETIRE]
        with (
            psycopg.connect(url, autocommit=True) as conn,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            conn.execute("UPDATE agents_meta SET status = 'restarting' WHERE id = 8")
