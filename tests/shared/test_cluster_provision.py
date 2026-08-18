"""Tests for shared.cluster.provision_database + drop_database (the cluster-DB
lifecycle pair).

Uses the native session DB (settings.data_plane.db_url, provisioned by
_provisioned_db in tests/conftest.py). The admin URL is derived by replacing
the database path component with 'postgres', matching the pattern used in
tests/ava/test_migrations.py.
"""

from __future__ import annotations

import os
import time

import psycopg
import pytest
from psycopg import sql

from shared.cluster import _swap_db, drop_database, provision_database
from shared.config import settings
from shared.url_secret import url_with_userinfo

# A URL-safe secret for the cluster role's password (provision_database creates
# an owning role sharing the db identifier and applies the schema as that role).
# The identifier is passed in full (names-as-data) — production callers read it
# from the cluster's own URLs or pass DATA_PLANE_IDENTITY at birth.
_SECRET = "provtestsecret"  # noqa: S105 — test fixture, not a real credential


def _admin_url() -> str:
    base, _ = settings.data_plane.db_url.rsplit("/", 1)
    return f"{base}/postgres"


def _fresh_identity() -> str:
    return f"ava_provtest_{os.getpid()}_{int(time.time() * 1_000_000)}"


def test_provision_database_creates_db_and_applies_schema(_provisioned_db: str) -> None:
    """provision_database creates the identity db, applies schema.sql (tables present)."""
    identity = _fresh_identity()
    expected_db = identity
    admin_url = _admin_url()
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    db_url = f"{base_url}/{expected_db}"

    try:
        provision_database(identity, base_admin_url=admin_url, cluster_secret=_SECRET)

        # Verify the database was created
        with psycopg.connect(admin_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)
            ).fetchone()
        assert row is not None, f"database {expected_db!r} was not created"

        # Verify schema was applied — check a known table exists
        with psycopg.connect(db_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables"
                "  WHERE table_schema = 'public' AND table_name = 'agents'"
                ")"
            ).fetchone()
        assert row is not None and row[0] is True, "table 'agents' not found — schema not applied"

    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pid <> pg_backend_pid()",
                (expected_db,),
            )
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(expected_db)))
            # provision_database also created the owning role (== expected_db).
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(expected_db)))


def test_drop_database_removes_db_idempotent(_provisioned_db: str) -> None:
    """drop_database removes the identity db; a second drop on the now-absent DB is a
    no-op (IF EXISTS), not an error."""
    identity = _fresh_identity()
    expected_db = identity
    admin_url = _admin_url()

    provision_database(identity, base_admin_url=admin_url, cluster_secret=_SECRET)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        assert (
            conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)).fetchone()
            is not None
        )

    drop_database(identity, base_admin_url=admin_url)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        assert (
            conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)).fetchone()
            is None
        ), f"database {expected_db!r} still present after drop_database"

    # idempotent: dropping an absent DB must not raise.
    drop_database(identity, base_admin_url=admin_url)


def test_provision_database_idempotent(_provisioned_db: str) -> None:
    """Calling provision_database twice does not raise and does not re-apply schema."""
    identity = _fresh_identity()
    expected_db = identity
    admin_url = _admin_url()

    try:
        provision_database(identity, base_admin_url=admin_url, cluster_secret=_SECRET)
        # Second call: DB already exists — must be a no-op, no exception.
        provision_database(identity, base_admin_url=admin_url, cluster_secret=_SECRET)

        # DB still exists after both calls.
        with psycopg.connect(admin_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)
            ).fetchone()
        assert row is not None

    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pid <> pg_backend_pid()",
                (expected_db,),
            )
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(expected_db)))
            # provision_database also created the owning role (== expected_db).
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(expected_db)))


def test_provisioned_role_is_nosuperuser_and_owns_db(_provisioned_db: str) -> None:
    """The cluster role is created LOGIN NOSUPERUSER and owns its
    database — so it bypasses no grant and reaches only its own cluster's data."""
    role = expected_db = _fresh_identity()  # db and role share the identifier
    admin_url = _admin_url()
    try:
        provision_database(role, base_admin_url=admin_url, cluster_secret=_SECRET)
        with psycopg.connect(admin_url, autocommit=True) as conn:
            attrs = conn.execute(
                "SELECT rolsuper, rolcanlogin FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            assert attrs == (False, True), "role must be LOGIN NOSUPERUSER"
            owner = conn.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (expected_db,)
            ).fetchone()
            assert owner is not None and owner[0] == role, "role must own its own database"
    finally:
        _drop_db_and_role(admin_url, expected_db)


def test_role_cannot_read_another_clusters_database(_provisioned_db: str) -> None:
    """Isolation: cluster B's role, connecting to cluster A's database, cannot read
    A's tables — it owns nothing there and holds no grant (NOSUPERUSER bypasses none)."""
    # Distinct suffixes off one base — two _fresh_identity() calls can collide on
    # the same microsecond, which would make a == b (one cluster) and void the test.
    base = _fresh_identity()
    a, b = f"{base}a", f"{base}b"
    admin_url = _admin_url()
    db_a, role_b = a, b
    try:
        provision_database(a, base_admin_url=admin_url, cluster_secret=_SECRET)
        provision_database(b, base_admin_url=admin_url, cluster_secret=_SECRET)
        # Connect to A's database AS B's role (loopback trust ignores the password).
        url_a_as_b = url_with_userinfo(_swap_db(admin_url, db_a), role_b, _SECRET)
        with (
            psycopg.connect(url_a_as_b, autocommit=True) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute("SELECT * FROM agents")
    finally:
        _drop_db_and_role(admin_url, a)
        _drop_db_and_role(admin_url, b)


def _drop_db_and_role(admin_url: str, name: str) -> None:
    """Terminate backends, DROP DATABASE, then DROP ROLE (the db + role share `name`)."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
