"""Isolated applied-set and Git fixtures for migration contracts."""

from __future__ import annotations

import os
import time
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from shared.config import settings
from shared.migrations import (
    _BASELINE_NAME,
    _MIGRATION_LOCK_KEY,
    required_migration_set,
)

_SCHEMA_SQL = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
_SYN = "29991231T235959_synthetic"
_SYN2 = "29991231T235960_synthetic-two"
SYN_ORPHAN = "20260815T000001_synthetic-orphan"


def _schema_sql_stamped_migration_names() -> list[str]:
    """The applied-set stamps db/schema.sql seeds, in file order.

    A folded strict migration keeps its stamp row so a fresh DB does not replay
    it. Reading the stamps from the file (rather than hard-coding a
    baseline+folded list) keeps this exact; the next folded migration cannot
    silently strand the fixture.
    """
    import re

    return re.findall(
        r"INSERT INTO schema_migrations \(name\) VALUES \('([^']+)'\)",
        _SCHEMA_SQL.read_text(),
    )


@contextmanager
def _throwaway_database(prefix: str) -> Generator[str, None, None]:
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_{prefix}_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def _set_table_to(
    conn: psycopg.Connection, shape: str, rows: Iterable[str] | Iterable[int] | None = None
) -> None:
    """Rebuild schema_migrations in `shape` ('set' or 'legacy') with `rows`."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS schema_migrations")
        if shape == "set":
            cur.execute(
                "CREATE TABLE schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            for name in rows or ():
                cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
        else:  # legacy
            cur.execute(
                "CREATE TABLE schema_migrations "
                "(version INT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            for v in rows or ():
                cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (v,))
    conn.commit()


@pytest.fixture(autouse=True)
def _reset_schema_migrations_state() -> Iterator[None]:
    """Rebuild schema_migrations to the canonical baselined state before/after each
    test. Rebuilds the table itself (not just TRUNCATE) so a cutover test that
    swapped it to the legacy shape cannot leak into the next test or module.

    conftest's `db_conn` TRUNCATE list does not include schema_migrations (it is
    not business data), so this module self-manages it — the same pattern the
    pre-cutover suite used.
    """

    def _reseed(names: list[str]) -> None:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            _set_table_to(conn, "set", names)

    # Captured at setup, not at teardown: a test's monkeypatch of MIGRATIONS_DIR
    # is still active when this autouse fixture tears down, and the after-state
    # must be the real checkout's canonical applied set.
    _canonical_applied = sorted(required_migration_set())
    _reseed([_BASELINE_NAME])
    yield
    # Restore the canonical fully-applied state (baseline + every migration file),
    # NOT a hard-coded prefix list: a freshly folded strict migration must stay
    # in the canonical applied set, or a later cmd_migrations_apply in the same
    # worker re-runs it against an already-current schema and fails loudly
    # (DuplicateColumn — the class that hit PR #1587's migration).
    _reseed(_canonical_applied)


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603 — test git fixture, fixed argv


def _init_repo(repo: Path) -> None:
    """Make `repo` a git worktree with an initial commit. The loader's
    git-tracking gate (#998) applies only what git tracks, so any test that
    monkeypatches MIGRATIONS_DIR must model a real checkout. Call AFTER writing
    the files that should be tracked (they land in the initial commit); files
    written afterwards are untracked by construction."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")


def _try_lock_from_other_conn() -> bool:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        row = cur.fetchone()
        assert row is not None
        got = row[0]
        if got:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
        return bool(got)
