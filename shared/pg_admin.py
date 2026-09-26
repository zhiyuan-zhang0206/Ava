"""Cluster Postgres admin authority — the one place DDL-capable dials are built.

Two forms, both over this home's owner-only Unix socket as the OS-user
bootstrap superuser (the initdb user):

- `connect` — the administrator as itself, for what only a superuser may do:
  roles, databases, extensions, grants on the cluster's behalf.
- `owner_session` / `OwnerAuthority` — the administrator ACTING AS the schema
  owner (`role=<owner>` in the startup options): schema baseline, checkpoint
  setup, migrations, start-time derived-cache DDL, and password-free
  owner-equivalent `pg_dump`. Objects are created owner-owned and privilege
  checks see exactly the owner's rights, but the owner itself never logs in,
  so it can later lose LOGIN without breaking these paths.

Moved down from `cli.commands._cluster_instance` (tech audit 2026-08-31, QA
#1133 P2 observation): the PITR services reach for the admin URL but must not
import up into `cli`. Everything here is `shared`-level (paths / cluster /
private-storage), so the admin dial lives beside the identity it serves.
"""

from __future__ import annotations

import getpass
import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import tuple_row

from shared.private_storage import ensure_private_dir

# The owner travels as a libpq startup option (`-c role=<owner>`), where spaces
# and backslashes are syntax. Cluster identities are plain identifiers; anything
# else is refused rather than escaped.
_PLAIN_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def pg_socket_dir(socket_root: Path | None = None, *, home: Path | None = None) -> Path:
    """A SHORT, cluster-unique socket directory. The Postgres socket path
    (`<dir>/.s.PGSQL.<port>`) is capped at 103 bytes, so it cannot live under a
    deep `$AVA_HOME` / pytest-tmp data dir — a short `/tmp/ava-pg-<home-slug>`
    (keyed on the cluster home path, never a name) stays well under the cap. The
    socket only serves local provisioning (the runtime connects over TCP); 0700
    keeps it owner-only. `home` defaults to `ava_home()` resolved in THIS module;
    the cli thin shell passes its own resolution so cli-layer steering (tests
    patch `cli.commands._cluster_instance.ava_home`) keeps flowing."""
    # Lazy: `shared.cluster` imports this module (provisioning dials through
    # it), so a module-level import would make `shared.pg_admin` unimportable
    # before `shared.cluster`.
    from shared.cluster.derive import home_slug

    if home is None:
        from shared.paths import ava_home

        home = ava_home()
    root = Path("/tmp") if socket_root is None else socket_root  # noqa: S108 — OS-fixed production socket root
    d = root / f"ava-pg-{home_slug(home)}"
    return ensure_private_dir(d)


def pg_admin_url(pg_port: int) -> str:
    """Dial only this home's canonical owner-only Unix socket directory."""
    return f"postgresql://{getpass.getuser()}@/postgres?host={pg_socket_dir()}&port={pg_port}"


@contextmanager
def connect(
    url: str, *, expected_data_dir: Path | None = None, **kwargs: Any
) -> Generator[psycopg.Connection[Any]]:
    """Open explicit admin authority; owned startup validates the actual connection.

    A caller managing native storage must supply its recorded data directory.
    Provider-managed provisioning helpers retain their explicit URL authority.
    Psycopg connections never reconnect, so validated custody lasts until close.
    """
    with psycopg.connect(url, **kwargs) as conn:
        if expected_data_dir is not None:
            from shared.cluster.ownership import require_postgres_connection

            require_postgres_connection(conn, expected_data_dir)
        yield conn


def owner_conninfo(admin_url: str, *, database: str, owner: str) -> str:
    """Conninfo for the administrator acting as `owner` on `database`.

    The role is a startup option rather than a later `SET ROLE`, so it is the
    session's own default: no transaction rollback can undo it, and a `RESET
    ROLE` returns to the owner, not to the superuser. libpq tools accept the
    same string (`pg_dump --dbname`), and no password is involved. A pooler that drops startup options would silently lose the
    role, so DDL goes through `owner_session`, which verifies it.

    Raises:
        ValueError: `owner` is not a plain identifier, or `admin_url` already
            carries startup options this would override.
    """
    if _PLAIN_ROLE.fullmatch(owner) is None:
        raise ValueError(f"schema owner {owner!r} is not a plain role identifier")
    if "options" in conninfo_to_dict(admin_url):
        raise ValueError("the admin URL must not carry its own startup options")
    return make_conninfo(admin_url, dbname=database, options=f"-c role={owner}")


@contextmanager
def owner_session(
    admin_url: str,
    *,
    database: str,
    owner: str,
    expected_data_dir: Path | None = None,
    **kwargs: Any,
) -> Generator[psycopg.Connection[Any]]:
    """Open the administrator acting as the schema owner — the DDL authority.

    Dials `owner_conninfo` with `connect`'s custody check, then proves the
    effective role before the caller runs anything: every object the caller
    creates is owned by `owner`, and every privilege check (DDL, GRANT, default
    privileges declared without `FOR ROLE`) sees the owner's rights, exactly as
    if the owner had logged in. The dial carries no statement ceiling.

    Raises:
        RuntimeError: the session's effective role is not `owner`.
    """
    conninfo = owner_conninfo(admin_url, database=database, owner=owner)
    with connect(conninfo, expected_data_dir=expected_data_dir, **kwargs) as conn:
        with conn.cursor(row_factory=tuple_row) as cursor:
            row = cursor.execute("SELECT current_user").fetchone()
        if row is None or row[0] != owner:
            raise RuntimeError(f"admin session did not assume schema owner {owner!r}")
        if not conn.autocommit:
            # Reading current_user opened a transaction; hand the caller a clean
            # session so its first statement starts its own transaction.
            conn.commit()
        yield conn


@dataclass(frozen=True)
class OwnerAuthority:
    """This home's administrator acting as its schema owner.

    `admin_url` dials the home's owner-only socket, `database` and `owner` are
    read as data from the cluster's own URL, and `data_dir` binds every session
    to the home's postmaster.
    """

    admin_url: str
    database: str
    owner: str
    data_dir: Path

    @property
    def conninfo(self) -> str:
        """Password-free conninfo for libpq tools and read-only verification."""
        return owner_conninfo(self.admin_url, database=self.database, owner=self.owner)

    @contextmanager
    def session(self, **kwargs: Any) -> Generator[psycopg.Connection[Any]]:
        """A custody-checked `owner_session` on this home's database."""
        with owner_session(
            self.admin_url,
            database=self.database,
            owner=self.owner,
            expected_data_dir=self.data_dir,
            **kwargs,
        ) as conn:
            yield conn


def local_owner_authority() -> OwnerAuthority:
    """Resolve this home's owner authority for its locally owned Postgres.

    The socket port comes from this home's registry record; the owner and
    database are the cluster URL's username and database (names as data).

    Raises:
        RuntimeError: the data plane is remote-managed (its provider URL is
            the only authority), or this home has no registry record.
    """
    from shared.cluster import db_identity, get_record, record_postgres_port
    from shared.config import settings
    from shared.paths import ava_home

    if settings.data_plane.is_remote:
        raise RuntimeError("a remote-managed data plane has no local owner authority")
    home = ava_home()
    record = get_record(home)
    if record is None:
        raise RuntimeError(f"no registry record for home {home}; cannot dial its Postgres")
    database = conninfo_to_dict(settings.data_plane.db_url).get("dbname")
    if not isinstance(database, str) or not database:
        raise RuntimeError("AVA_DB_URL names no database")
    return OwnerAuthority(
        admin_url=pg_admin_url(record_postgres_port(record)),
        database=database,
        owner=db_identity(),
        data_dir=home / "pg",
    )
