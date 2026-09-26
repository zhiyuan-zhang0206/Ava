"""CI-only installed-wheel/native-PG migration authority and start-admission proof.

This exercises the migration contract on an isolated database. It does not prove
schema-changing release rollout, a writer barrier, or successful service startup.
"""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import LiteralString
from unittest.mock import patch

import psycopg

from cli.commands.start import cmd_start
from cli.start_runtime import StartRuntime
from shared import ui_update_state
from shared.config import settings
from shared.machine import machine_name
from shared.migrations import (
    MIGRATIONS_DIR,
    MigrationAuthorityMismatch,
    applied_migration_names,
    apply_pending_migrations,
    check_schema_version,
    required_migration_set,
)
from shared.release_identity import ApplicationIdentity
from shared.runtime_migration import ReleaseMigrationContext, installed_migration_paths
from shared.runtime_release import ReleaseRejectedError, current_pointer, verify_release
from shared.verified_file import regular_bytes


def require(condition: bool, message: str) -> None:  # noqa: FBT001 — proof predicate.
    if not condition:
        raise AssertionError(message)


def rejected(action: Callable[[], object]) -> None:
    try:
        action()
    except (ReleaseRejectedError, MigrationAuthorityMismatch):
        return
    raise AssertionError("invalid migration authority was accepted")


def reject_unchanged(conn: psycopg.Connection, context: ReleaseMigrationContext) -> None:
    before = applied_migration_names(conn)
    rejected(lambda: apply_pending_migrations(conn, release=context))
    require(applied_migration_names(conn) == before, "refused migration changed schema history")


def home_inputs(home: Path) -> dict[Path, bytes]:
    # Lifecycle serialization records lock custody even when startup refuses.
    # Those two coordination files are not identity or configuration writes.
    lock = ui_update_state.lifecycle_lock_path()
    coordination = {lock, lock.with_name(lock.name + ".holder.json")}
    return {
        path: path.read_bytes()
        for path in home.iterdir()
        if path.is_file() and path not in coordination
    }


def prove_start_barrier(runtime: StartRuntime) -> None:
    """An inactive verified image lacks start admission despite valid migration authority."""
    home = runtime.home
    if home is None or runtime.release is None:
        raise AssertionError("proof needs a captured release")
    require(current_pointer(home / "releases") is None, "proof home must have no selected image")
    require(not (home / "start-intent.json").exists(), "proof home must remain uninitialized")
    before = home_inputs(home)
    with patch(
        "cli.commands._setup._collect_setup_values",
        side_effect=AssertionError("unadmitted start reached setup"),
    ):
        try:
            cmd_start(runtime=runtime)
        except ReleaseRejectedError as exc:
            require("selected captured image" in str(exc), "start refused at an unexpected gate")
        else:
            raise AssertionError("migration authority incorrectly authorized service startup")
    after = home_inputs(home)
    require(after == before, "refused start changed home identity/configuration")
    require(current_pointer(home / "releases") is None, "refused start selected an image")


def prove_authority(conn: psycopg.Connection, context: ReleaseMigrationContext) -> None:
    """Exercise the real applier, preserving history for each rejected authority."""
    reject_unchanged(conn, replace(context, home=context.home.parent))
    changes: tuple[tuple[LiteralString, tuple[str, ...]], ...] = (
        ("UPDATE machine_units SET machine_name=%s", ("wrong-unit",)),
        ("UPDATE deployment_state SET holder=%s", ("different-operation",)),
        ("UPDATE deployment_state SET acquired_at=acquired_at + interval '1 second'", ()),
        ("UPDATE deployment_state SET expires_at=now() - interval '1 second'", ()),
        ("UPDATE deployment_state SET target_sha=%s", ("d" * 40,)),
    )
    for statement, parameters in changes:
        with conn.transaction(force_rollback=True):
            conn.execute(statement, parameters)
            reject_unchanged(conn, context)
    reject_unchanged(
        conn, replace(context, release=replace(context.release, manifest_digest="0" * 64))
    )
    sql = next(MIGRATIONS_DIR.glob("*.sql"))
    original, mode = sql.read_bytes(), stat.S_IMODE(sql.stat().st_mode)
    try:
        sql.chmod(0o600)
        sql.write_bytes(original + b"\n-- injected CI corruption\n")
        rejected(lambda: installed_migration_paths(MIGRATIONS_DIR))
        reject_unchanged(conn, context)
    finally:
        sql.write_bytes(original)
        sql.chmod(mode)
    require(apply_pending_migrations(conn, release=context) == [], "restored SQL was not a no-op")


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
    identity = ApplicationIdentity.model_validate_json(
        regular_bytes(MIGRATIONS_DIR.parent / "shared/release-build.json")
    )
    runtime = StartRuntime.from_image(
        home, release, schema_digest=schema_digest, source_commit=identity.source_commit
    )
    required = required_migration_set()
    with psycopg.connect(settings.data_plane.db_url) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        conn.execute(
            "CREATE TABLE machine_units(machine_name text, home text, serve_gateway boolean)"
        )
        conn.execute(
            "CREATE TABLE deployment_state(id integer PRIMARY KEY, holder text, acquired_at timestamptz, expires_at timestamptz, kind text, phase text, target_sha text, settle_started_at timestamptz, settle_hosts text[], settle_note text)"
        )
        for name in required:
            conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (name,))
        conn.execute("INSERT INTO machine_units VALUES (%s,%s,true)", (machine_name(), str(home)))
        row = conn.execute(
            "INSERT INTO deployment_state(id,holder,acquired_at,expires_at,kind,phase,target_sha) VALUES (1,'ci:pid1',now(),now()+interval '10 minutes','rollout','updating',%s) RETURNING acquired_at",
            (identity.source_commit,),
        ).fetchone()
        if row is None:
            raise AssertionError("fixture lease was not created")
        context = ReleaseMigrationContext(release, home, "ci:pid1", row[0], identity.source_commit)
        require(
            apply_pending_migrations(conn, release=context) == [], "current SQL was not a no-op"
        )
        check_schema_version(conn)
        prove_authority(conn, context)
        prove_start_barrier(runtime)
        require(applied_migration_names(conn) == required, "proof changed schema history")
        print(
            json.dumps(
                {
                    "installed_wheel_migration_authority": True,
                    "loaded_image_provenance": True,
                    "readonly_schema_check": True,
                    "wrong_home_unit_lease_target_sql_rejected": True,
                    "manifest_and_package_inventory_enforced": True,
                    "existing_schema_apply_was_noop": True,
                    "migration_authority_does_not_admit_start": True,
                }
            )
        )


if __name__ == "__main__":
    main()
