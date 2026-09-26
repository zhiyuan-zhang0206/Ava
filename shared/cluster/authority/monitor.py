"""The stable monitoring login: the OTel collector's PostgreSQL receiver.

``ava_monitor`` is not a write generation. It holds no application privilege,
so a rollout neither revokes nor re-delivers it, and the fence never closes
its sessions. It has no password: ``pg_hba`` admits it only by ``peer`` on the
home's owner-only socket, mapped from the home's OS user (``pg_ident`` map
``ava_monitor``), and SCRAM can never authenticate a role without a verifier.
No credential therefore exists to store in the collector configuration.

Its whole privilege surface, verified by the invariant:

- ``LOGIN INHERIT``, nothing elevated, no settings, no limit or expiry, no
  password; owns nothing and has no members;
- exactly one membership, ``pg_read_all_stats`` (``INHERIT TRUE, SET FALSE``):
  other sessions' ``pg_stat_activity``/``pg_stat_replication`` rows and every
  database's size;
- exactly one direct grant, ``CONNECT`` on the cluster database (PUBLIC lost
  it). Statistics views and ``pg_relation_size`` need no table privilege, so
  it reads no application row.
"""

from __future__ import annotations

from psycopg import sql

from shared.cluster.authority.catalog import (
    Conn,
    RoleFacts,
    memberships,
    require_admin,
    role_facts,
)
from shared.cluster.authority.model import AuthorityRefusedError, CatalogRefusedError

MONITOR_ROLE = "ava_monitor"
# The pg_ident map pg_hba names for the monitor's peer line.
MONITOR_MAP = "ava_monitor"
_STATS_ROLE = "pg_read_all_stats"

_DIRECT_GRANTS = """
SELECT d.dbid, d.classid::regclass::text, d.objid, d.deptype
FROM pg_shdepend d JOIN pg_authid a ON a.oid = d.refobjid
WHERE d.refclassid = 'pg_authid'::regclass AND a.rolname = %s
ORDER BY 1, 2, 3, 4
"""


def _attribute_violations(fact: RoleFacts) -> list[str]:
    name = fact.name
    violations: list[str] = []
    if not fact.login:
        violations.append(f"{name} cannot log in")
    if fact.password is not None:
        violations.append(f"{name} has a password; it authenticates only by peer")
    if fact.elevated or not fact.inherit or fact.configured:
        violations.append(f"{name} holds attributes, NOINHERIT or settings")
    if fact.connection_limit != -1 or fact.valid_until is not None:
        violations.append(f"{name} carries a connection limit or expiry")
    return violations


def _grant_violations(conn: Conn, database: str) -> list[str]:
    name = MONITOR_ROLE
    violations: list[str] = []
    rows = memberships(conn, (name,))
    held = [(row.role, row.admin, row.inherit, row.set) for row in rows if row.member == name]
    if held != [(_STATS_ROLE, False, True, False)]:
        violations.append(f"{name} memberships {held} differ from INHERIT-only {_STATS_ROLE}")
    if any(row.role == name for row in rows):
        violations.append(f"{name} has members")
    row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (database,)).fetchone()
    expected = [(0, "pg_database", None if row is None else row[0], "a")]
    grants = [tuple(grant) for grant in conn.execute(_DIRECT_GRANTS, (name,)).fetchall()]
    if grants != expected:
        violations.append(f"{name} owns objects or holds grants beyond CONNECT on {database}")
    return violations


def monitor_violations(conn: Conn, *, database: str) -> list[str]:
    """Why the existing monitor role is not exactly its shape (absent: none)."""
    fact = role_facts(conn, (MONITOR_ROLE,)).get(MONITOR_ROLE)
    if fact is None:
        return []
    return _attribute_violations(fact) + _grant_violations(conn, database)


def ensure_monitor(conn: Conn, *, database: str) -> None:
    """Create the monitor login if missing and converge its two grants.

    Idempotent. An existing role of any other shape is an unknown effect and
    refuses; a missing one is created password-less.
    """
    require_admin(conn)
    row = conn.execute("SELECT current_database()").fetchone()
    if row is None or row[0] != database:
        raise AuthorityRefusedError("the monitor role must converge in the cluster database")
    role = sql.Identifier(MONITOR_ROLE)
    with conn.transaction():
        if MONITOR_ROLE not in role_facts(conn, (MONITOR_ROLE,)):
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE"
                    " NOREPLICATION NOBYPASSRLS PASSWORD NULL"
                ).format(role)
            )
        conn.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE").format(
                sql.Identifier(_STATS_ROLE), role
            )
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role)
        )
        violations = monitor_violations(conn, database=database)
        if violations:
            raise CatalogRefusedError(tuple(violations))
