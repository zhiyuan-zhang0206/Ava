"""CI-only installed-wheel to native-PG migration admission proof."""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import psycopg

from cli.commands._release_candidate import load_candidate, record_candidate
from cli.commands._update_git import apply_pending_migrations
from cli.commands.start import cmd_start
from shared.config import settings
from shared.machine import machine_name
from shared.migrations import (
    MIGRATIONS_DIR,
    MigrationAuthorityMismatch,
    check_schema_version,
    required_migration_set,
)
from shared.runtime_migration import ReleaseMigrationContext
from shared.runtime_release import ReleaseRejectedError, file_sha256, verify_release


def rejected(action: Callable[[], object]) -> None:
    try:
        action()
    except (ReleaseRejectedError, MigrationAuthorityMismatch):
        return
    raise AssertionError("invalid candidate was accepted")


def require(condition: bool, message: str) -> None:  # noqa: FBT001 — proof predicate.
    if not condition:
        raise AssertionError(message)


def prove_start_barrier(receipt: Path) -> None:
    try:
        cmd_start(release_receipt=receipt)
    except ReleaseRejectedError as exc:
        require("service closure" in str(exc), "start did not reach its release closure gate")
    else:
        raise AssertionError("a migration receipt incorrectly authorized service cutover")


def main() -> None:
    home = Path(settings.general.ava_home).resolve()
    if os.environ.get("GITHUB_ACTIONS") != "true" or not home.is_relative_to(
        Path(os.environ["RUNNER_TEMP"]).resolve()
    ):
        raise RuntimeError("native migration proof is restricted to GitHub runner scratch space")
    artifact, manifest_digest, schema_digest = sys.argv[1:]
    release = verify_release(
        home / "releases",
        artifact,
        manifest_digest=manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=schema_digest,
    )
    required = required_migration_set()
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        conn.execute(
            "CREATE TABLE machine_units(machine_name text, home text, serve_gateway boolean)"
        )
        conn.execute(
            "CREATE TABLE deployment_state(id integer PRIMARY KEY, holder text, acquired_at timestamptz, expires_at timestamptz, note text, kind text, phase text, target_sha text, settle_started_at timestamptz)"
        )
        for name in required:
            conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (name,))
        conn.execute("INSERT INTO machine_units VALUES (%s,%s,true)", (machine_name(), str(home)))
        row = conn.execute(
            "INSERT INTO deployment_state(id,holder,acquired_at,expires_at,kind,phase,target_sha) VALUES (1,'ci:pid1',now(),now()+interval '10 minutes','rollout','updating',%s) RETURNING acquired_at",
            ("c" * 40,),
        ).fetchone()
        if row is None:
            raise AssertionError("fixture lease was not created")
        context = ReleaseMigrationContext(release, home, "ci:pid1", row[0], "c" * 40)
        receipt = record_candidate(conn, context, schema_digest=schema_digest)
        require(stat.S_IMODE(receipt.stat().st_mode) == 0o600, "receipt mode is not private")
        require(
            record_candidate(conn, context, schema_digest=schema_digest) == receipt,
            "receipt is not idempotent",
        )
        admitted = load_candidate(conn, receipt)
        require(
            apply_pending_migrations(release=admitted) == [], "current schema unexpectedly changed"
        )
        check_schema_version(conn)

        # These are admission failures against the real DB, not mock callbacks.
        rejected(
            lambda: record_candidate(
                conn, replace(context, home=home.parent), schema_digest=schema_digest
            )
        )
        conn.execute("UPDATE machine_units SET machine_name='wrong-unit'")
        rejected(lambda: load_candidate(conn, receipt))
        conn.execute("UPDATE machine_units SET machine_name=%s", (machine_name(),))
        conn.execute("UPDATE deployment_state SET holder='different-operation'")
        rejected(lambda: load_candidate(conn, receipt))
        conn.execute("UPDATE deployment_state SET holder='ci:pid1'")
        conn.execute("UPDATE deployment_state SET target_sha=%s", ("d" * 40,))
        rejected(lambda: load_candidate(conn, receipt))
        conn.execute("UPDATE deployment_state SET target_sha=%s", ("c" * 40,))
        sql = next(MIGRATIONS_DIR.glob("*.sql"))
        original, mode = sql.read_bytes(), stat.S_IMODE(sql.stat().st_mode)
        try:
            sql.chmod(0o600)
            sql.write_bytes(original + b"\n-- injected CI corruption\n")
            rejected(lambda: load_candidate(conn, receipt))
        finally:
            sql.write_bytes(original)
            sql.chmod(mode)
        require(load_candidate(conn, receipt).release.digest == artifact, "restored image rejected")
        prove_start_barrier(receipt)
        rows = conn.execute("SELECT name FROM schema_migrations").fetchall()
        require({row[0] for row in rows} == required, "negative admission changed schema history")
        print(
            json.dumps(
                {
                    "wheel_to_pg_cli_admission": True,
                    "readonly_schema_check": True,
                    "wrong_home_unit_lease_target_sql_rejected": True,
                    "existing_schema_apply_was_noop": True,
                    "actual_start_preserves_cutover_barrier": True,
                    "receipt_sha256": file_sha256(receipt),
                }
            )
        )


if __name__ == "__main__":
    main()
