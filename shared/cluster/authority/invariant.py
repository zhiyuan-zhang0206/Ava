"""The read-only authority invariant: the catalog must equal the ledger.

Fail closed. Every unknown effect is a violation, and all violations are
reported together in one ``CatalogRefusedError``:

- the owner is NOLOGIN, passwordless, not a superuser, owns the database and
  has no memberships;
- the groups are NOLOGIN capability holders that own nothing;
- the groups' members, transitively, are exactly the unrevoked generation's
  logins, and each is exactly shaped (``roles.verify_generation``);
- every recorded revoked login that still exists is an inert tombstone;
- every other login (auxiliary: replication, operator read-only) is not a
  superuser, owns nothing and holds no write privilege on ``public``;
- every ACL grantee on the database, schema ``public`` and its objects, and
  every default privilege, is the object's owner, the schema owner, a group,
  PUBLIC with only EXECUTE/USAGE/TEMPORARY, or an allowlisted read-only role
  with only SELECT/USAGE/CONNECT; PUBLIC does not hold CONNECT.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from shared.cluster.authority.catalog import (
    BOOTSTRAP_SUPERUSER_OID,
    Conn,
    group_members,
    login_roles,
    memberships,
    require_admin,
    role_facts,
    transitive_members,
)
from shared.cluster.authority.groups import group_violations
from shared.cluster.authority.ledger import require_ledger
from shared.cluster.authority.model import (
    AuthorityRefusedError,
    CatalogRefusedError,
    Ledger,
    VerifiedGeneration,
)
from shared.cluster.authority.roles import verify_generation

_PUBLIC_ALLOWED = frozenset({"EXECUTE", "USAGE", "TEMPORARY"})
_READONLY_ALLOWED = frozenset({"SELECT", "USAGE", "CONNECT"})


def _owner_violations(conn: Conn, ledger: Ledger, database: str) -> list[str]:
    fact = role_facts(conn, (ledger.owner,)).get(ledger.owner)
    if fact is None:
        return [f"schema owner {ledger.owner} does not exist"]
    violations: list[str] = []
    if fact.login or fact.password is not None:
        violations.append(f"schema owner {ledger.owner} can log in")
    if fact.oid == BOOTSTRAP_SUPERUSER_OID or fact.elevated or fact.configured:
        violations.append(f"schema owner {ledger.owner} holds attributes or settings")
    if memberships(conn, (ledger.owner,)):
        violations.append(f"schema owner {ledger.owner} has memberships")
    row = conn.execute(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (database,)
    ).fetchone()
    if row is None or row[0] != ledger.owner:
        violations.append(f"database {database} is not owned by {ledger.owner}")
    return violations


def _membership_violations(conn: Conn, ledger: Ledger) -> list[str]:
    expected: set[str] = set(ledger.unrevoked.roles) if ledger.unrevoked is not None else set()
    extra = sorted(set(group_members(conn, ledger.groups)) - expected)
    violations = [f"unexpected group members {extra}"] if extra else []
    owner_members = transitive_members(conn, (ledger.owner,))
    if owner_members:
        violations.append(f"roles hold schema-owner membership: {list(owner_members)}")
    return violations


def _revoked_violations(conn: Conn, ledger: Ledger) -> list[str]:
    names = [name for entry in ledger.revoked for name in entry.roles]
    violations = [
        f"revoked login {name} can log in"
        for name, fact in role_facts(conn, names).items()
        if fact.login or fact.password is not None
    ]
    violations += [
        f"revoked login {row.member} is a member of {row.role}"
        for row in memberships(conn, names)
        if row.member in names
    ]
    return violations


_WRITES = """
SELECT c.oid::regclass::text, p.privilege
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) AS p(privilege)
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND has_table_privilege(%(role)s, c.oid, p.privilege)
UNION ALL
SELECT c.oid::regclass::text, p.privilege
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['USAGE', 'UPDATE']) AS p(privilege)
WHERE n.nspname = 'public' AND c.relkind = 'S'
  AND has_sequence_privilege(%(role)s, c.oid, p.privilege)
ORDER BY 1, 2
LIMIT 5
"""


def _auxiliary_violations(conn: Conn, ledger: Ledger) -> list[str]:
    active: set[str] = set(ledger.unrevoked.roles) if ledger.unrevoked is not None else set()
    violations: list[str] = []
    for name, fact in sorted(login_roles(conn).items()):
        if fact.oid == BOOTSTRAP_SUPERUSER_OID or name in active:
            continue
        if fact.superuser:
            violations.append(f"login {name} is an unexpected superuser")
            continue
        if fact.owns:
            violations.append(f"login {name} owns objects")
        writes = conn.execute(_WRITES, {"role": name}).fetchall()
        if writes:
            violations.append(f"login {name} holds write privileges {writes}")
    return violations


_ACLS = """
WITH acls AS (
    SELECT 'relation ' || c.oid::regclass::text AS object, c.relowner AS owner, a.grantee,
           a.privilege_type
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a
    WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'column ' || c.oid::regclass::text || '.' || att.attname, c.relowner, a.grantee,
           a.privilege_type
    FROM pg_attribute att
    JOIN pg_class c ON c.oid = att.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(att.attacl) a
    WHERE n.nspname = 'public' AND att.attnum > 0 AND NOT att.attisdropped
    UNION ALL
    SELECT 'routine ' || p.oid::regprocedure::text, p.proowner, a.grantee, a.privilege_type
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a
    WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'schema public', n.nspowner, a.grantee, a.privilege_type
    FROM pg_namespace n, aclexplode(n.nspacl) a
    WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'database ' || d.datname, d.datdba, a.grantee, a.privilege_type
    FROM pg_database d, aclexplode(d.datacl) a
    WHERE d.datname = current_database()
)
SELECT object, CASE WHEN grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee) END,
       privilege_type, grantee = owner
FROM acls
ORDER BY 1, 2, 3
"""

_DEFAULT_ACLS = """
SELECT pg_get_userbyid(d.defaclrole),
       CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
       a.privilege_type, a.grantee = d.defaclrole
FROM pg_default_acl d, aclexplode(d.defaclacl) a
ORDER BY 1, 2, 3
"""


def _grant_allowed(
    ledger: Ledger, grantee: str, privilege: str, *, own: bool, readonly: frozenset[str]
) -> bool:
    if own or grantee in {ledger.owner, ledger.groups.gateway, ledger.groups.runner}:
        return True
    if grantee == "PUBLIC":
        return privilege in _PUBLIC_ALLOWED
    return grantee in readonly and privilege in _READONLY_ALLOWED


def _acl_violations(conn: Conn, ledger: Ledger, readonly: frozenset[str]) -> list[str]:
    violations = [
        f"{target} grants {privilege} to {grantee}"
        for target, grantee, privilege, own in conn.execute(_ACLS)
        if not _grant_allowed(ledger, grantee, privilege, own=own, readonly=readonly)
    ]
    for role, grantee, privilege, own in conn.execute(_DEFAULT_ACLS):
        if role != ledger.owner:
            violations.append(f"default privileges declared for {role}")
        elif not _grant_allowed(ledger, grantee, privilege, own=own, readonly=readonly):
            violations.append(f"default privileges grant {privilege} to {grantee}")
    row = conn.execute(
        "SELECT has_database_privilege('public', current_database(), 'CONNECT')"
    ).fetchone()
    if row is None or row[0]:
        violations.append("PUBLIC may connect to the cluster database")
    return violations


def check_invariant(
    conn: Conn, home: Path, *, database: str, readonly_grantees: Iterable[str] = ()
) -> VerifiedGeneration | None:
    """Prove the catalog equals the ledger; return the unrevoked generation's receipt.

    Read-only. ``conn`` must be the admin session connected to ``database``.
    ``readonly_grantees`` names operator roles allowed SELECT/USAGE/CONNECT.
    """
    require_admin(conn)
    row = conn.execute("SELECT current_database()").fetchone()
    if row is None or row[0] != database:
        raise AuthorityRefusedError("the invariant must run connected to the cluster database")
    ledger = require_ledger(home)
    readonly = frozenset(readonly_grantees)
    violations = _owner_violations(conn, ledger, database)
    violations += group_violations(conn, ledger.groups)
    violations += [
        f"capability group {name} does not exist"
        for name in (ledger.groups.gateway, ledger.groups.runner)
        if name not in role_facts(conn, (name,))
    ]
    violations += _membership_violations(conn, ledger)
    violations += _revoked_violations(conn, ledger)
    violations += _auxiliary_violations(conn, ledger)
    violations += _acl_violations(conn, ledger, readonly)
    verified: VerifiedGeneration | None = None
    if ledger.unrevoked is not None:
        try:
            verified = verify_generation(conn, home)
        except CatalogRefusedError as refusal:
            violations += refusal.violations
    if violations:
        raise CatalogRefusedError(tuple(violations))
    return verified
