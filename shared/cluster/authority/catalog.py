"""Read-only PostgreSQL catalog facts the authority library decides on.

Every function takes the caller's admin connection: the OS-user superuser
session over the owner-only socket, validated for custody by its opener. This
module never opens a connection and never infers authority from a role name.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from shared.cluster.authority.model import AuthorityRefusedError, Groups

BOOTSTRAP_SUPERUSER_OID = 10

Conn = psycopg.Connection[Any]


def require_admin(conn: Conn) -> None:
    """Refuse anything but an autocommit superuser session without SET ROLE.

    Autocommit makes each authority step its own committed transaction, so a
    durable boundary is exactly where the code says it is.
    """
    if not conn.autocommit:
        raise AuthorityRefusedError("database authority requires an autocommit admin session")
    row = conn.execute(
        "SELECT r.rolsuper, session_user = current_user"
        " FROM pg_roles r WHERE r.rolname = session_user"
    ).fetchone()
    if row != (True, True):
        raise AuthorityRefusedError(
            "database authority requires the superuser session without SET ROLE"
        )


@dataclass(frozen=True)
class RoleFacts:
    oid: int
    name: str
    login: bool
    superuser: bool
    inherit: bool
    createrole: bool
    createdb: bool
    replication: bool
    bypassrls: bool
    connection_limit: int
    valid_until: datetime | None
    password: str | None
    configured: bool
    depended: bool
    owns: bool

    @property
    def elevated(self) -> tuple[str, ...]:
        flags = {
            "SUPERUSER": self.superuser,
            "CREATEROLE": self.createrole,
            "CREATEDB": self.createdb,
            "REPLICATION": self.replication,
            "BYPASSRLS": self.bypassrls,
        }
        return tuple(name for name, held in flags.items() if held)


_ROLE_FACTS = """
SELECT a.oid, a.rolname, a.rolcanlogin, a.rolsuper, a.rolinherit, a.rolcreaterole,
       a.rolcreatedb, a.rolreplication, a.rolbypassrls, a.rolconnlimit, a.rolvaliduntil,
       a.rolpassword,
       EXISTS (SELECT 1 FROM pg_db_role_setting s WHERE s.setrole = a.oid),
       EXISTS (SELECT 1 FROM pg_shdepend d
               WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = a.oid),
       EXISTS (SELECT 1 FROM pg_shdepend d
               WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = a.oid
                 AND d.deptype = 'o')
FROM pg_authid a
WHERE a.rolname = ANY(%s)
"""


def role_facts(conn: Conn, names: Iterable[str]) -> dict[str, RoleFacts]:
    """Catalog facts for the named roles that exist (absent names are omitted)."""
    rows = conn.execute(_ROLE_FACTS, (sorted(set(names)),)).fetchall()
    return {row[1]: RoleFacts(*row) for row in rows}


def login_roles(conn: Conn) -> dict[str, RoleFacts]:
    """Facts for every role that can currently log in."""
    names = [row[0] for row in conn.execute("SELECT rolname FROM pg_roles WHERE rolcanlogin")]
    return role_facts(conn, names)


@dataclass(frozen=True)
class Membership:
    role: str
    member: str
    grantor: str
    admin: bool
    inherit: bool
    set: bool


_MEMBERSHIPS = """
SELECT g.rolname, m.rolname, gr.rolname, am.admin_option, am.inherit_option, am.set_option
FROM pg_auth_members am
JOIN pg_roles g ON g.oid = am.roleid
JOIN pg_roles m ON m.oid = am.member
JOIN pg_roles gr ON gr.oid = am.grantor
WHERE g.rolname = ANY(%(names)s) OR m.rolname = ANY(%(names)s)
ORDER BY 1, 2, 3
"""


def memberships(conn: Conn, names: Iterable[str]) -> tuple[Membership, ...]:
    """Every membership row in which a named role is the role or the member."""
    rows = conn.execute(_MEMBERSHIPS, {"names": sorted(set(names))}).fetchall()
    return tuple(Membership(*row) for row in rows)


_TRANSITIVE_MEMBERS = """
WITH RECURSIVE members(oid) AS (
    SELECT am.member FROM pg_auth_members am
    JOIN pg_roles g ON g.oid = am.roleid
    WHERE g.rolname = ANY(%s)
    UNION
    SELECT am.member FROM pg_auth_members am JOIN members ON am.roleid = members.oid
)
SELECT r.rolname FROM members JOIN pg_roles r ON r.oid = members.oid ORDER BY 1
"""


def transitive_members(conn: Conn, roles: Iterable[str]) -> tuple[str, ...]:
    """Every role holding membership in ``roles``, directly or through another role."""
    return tuple(row[0] for row in conn.execute(_TRANSITIVE_MEMBERS, (sorted(set(roles)),)))


def group_members(conn: Conn, groups: Groups) -> tuple[str, ...]:
    return transitive_members(conn, (groups.gateway, groups.runner))
