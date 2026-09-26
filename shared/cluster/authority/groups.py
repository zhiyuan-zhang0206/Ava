"""Stable NOLOGIN capability groups and their grant surface.

Every application privilege is granted to one of two groups, never to a login:

- ``ava_gateway``: the owner's DML surface without DDL or ownership. SELECT,
  INSERT, UPDATE, DELETE on every table; USAGE, SELECT, UPDATE on every
  sequence; EXECUTE on every routine, including those revoked from PUBLIC;
  PostgreSQL 17 ``MAINTAIN`` on the checkpoint tables the blob vacuum
  maintains. No TRUNCATE, REFERENCES or TRIGGER.
- ``ava_runner``: the audited runner matrix. The historical ``ava_runner``
  login is demoted in place, so grants recorded by the schema and migrations
  keep working.

Both groups hold CONNECT on the cluster database, which PUBLIC loses, and
USAGE on schema ``public``. Standing ``ALTER DEFAULT PRIVILEGES FOR ROLE
<owner>`` covers objects later migrations create as the owner; the ``ALL``
grants are point-in-time loops, so re-running ``ensure_groups`` after a
migration closes the retroactive half.
"""

from __future__ import annotations

from typing import LiteralString

from psycopg import sql
from psycopg.errors import Diagnostic

from shared.agents.impersonation_manifest_grants import grant_manifest_runner_access
from shared.cluster.authority.catalog import (
    BOOTSTRAP_SUPERUSER_OID,
    Conn,
    memberships,
    require_admin,
    role_facts,
)
from shared.cluster.authority.model import (
    AuthorityRefusedError,
    BirthAuthority,
    CatalogRefusedError,
    CutoverAuthority,
    Groups,
)

CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

# The runner matrix: (privileges, tables). Privilege strings are module
# constants spliced as SQL; table names are quoted identifiers. Each entry is a
# write path a runner process performs directly (not through the gateway API).
_RUNNER_TABLE_GRANTS: tuple[tuple[LiteralString, tuple[str, ...]], ...] = (
    # Claim polling and agent status/liveness; INSERT on agents_meta stays with spawn.
    ("SELECT, UPDATE", ("inbound_messages", "agents_meta")),
    # Agent-side self-lifecycle inbounds (terminate / restart / compact).
    ("INSERT", ("inbound_messages",)),
    # ava.self.set_label updates the agent's own row.
    ("UPDATE", ("agents",)),
    # register_self / mark_stopping and the deploy posture on every start.
    ("INSERT, UPDATE, SELECT", ("machine_units",)),
    ("INSERT, UPDATE", ("machines", "host_deploy_state")),
    # The runner ops server dedupes inbound /ops calls.
    ("INSERT, UPDATE, DELETE", ("api_idempotency",)),
    # SDK surfaces the runner writes directly: ava.tasks, ava.watcher, impersonation.
    ("INSERT, UPDATE", ("agent_tasks", "agent_watchers", "agent_impersonation_messages")),
    # Understanding-tree regeneration reconciles superseded cuts (task #3704).
    ("SELECT, INSERT, UPDATE, DELETE", ("understanding_nodes",)),
    # Compact-boundary tree-build enqueue (task #4674).
    ("INSERT", ("hierarchy_jobs",)),
    # A cleanly exiting watcher deletes its own registry row.
    ("DELETE", ("agent_watchers",)),
    # Page close at exit.
    ("UPDATE", ("agent_pages",)),
    # Shell TTL deadlines and their append-only renewal trail.
    ("INSERT, UPDATE", ("agent_shell_ttls",)),
    ("SELECT, INSERT", ("agent_shell_ttl_renewals",)),
    # ava.self.pause_heartbeat append-only trail (task #1932).
    ("SELECT, INSERT", ("heartbeat_pause_log",)),
    # Plugin statistics cards; stale rows age in place, never deleted.
    ("SELECT, INSERT, UPDATE", ("plugin_stats",)),
    ("SELECT, INSERT", ("agent_metric_observations",)),
    (
        "SELECT, INSERT, UPDATE",
        (
            "agent_metric_days",
            "agent_lifecycle_intervals",
            "agent_metric_scans",
            "agent_metric_file_cursors",
        ),
    ),
    # Start-readiness alert upsert and in-place resolution (task #3747).
    ("SELECT, INSERT, UPDATE", ("alerts",)),
    # Agent state: the LangGraph checkpoint tables.
    ("ALL", CHECKPOINT_TABLES),
)


def group_violations(conn: Conn, groups: Groups) -> list[str]:
    names = (groups.gateway, groups.runner)
    facts = role_facts(conn, names)
    violations: list[str] = []
    for name, fact in facts.items():
        if fact.login or fact.password is not None:
            violations.append(
                f"group {name} can log in; an existing home converts through the cutover"
            )
        if fact.elevated or fact.owns or fact.configured:
            violations.append(f"group {name} holds attributes, ownership or settings")
        if not fact.inherit:
            violations.append(f"group {name} is NOINHERIT")
    for row in memberships(conn, names):
        if row.member in names:
            violations.append(f"group {row.member} is a member of {row.role}")
    return violations


def _create_missing(conn: Conn, groups: Groups) -> None:
    existing = role_facts(conn, (groups.gateway, groups.runner))
    for name in (groups.gateway, groups.runner):
        if name not in existing:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE"
                    " NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(name))
            )


def _grant(conn: Conn, template: LiteralString, *identifiers: str) -> None:
    conn.execute(sql.SQL(template).format(*(sql.Identifier(name) for name in identifiers)))


def _grant_common(conn: Conn, database: str, groups: Groups) -> None:
    for group in (groups.gateway, groups.runner):
        _grant(conn, "GRANT CONNECT ON DATABASE {} TO {}", database, group)
        _grant(conn, "GRANT USAGE ON SCHEMA public TO {}", group)
    _grant(conn, "REVOKE CONNECT ON DATABASE {} FROM PUBLIC", database)


def _grant_gateway(conn: Conn, owner: str, gateway: str) -> None:
    _grant(
        conn, "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}", gateway
    )
    _grant(conn, "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}", gateway)
    _grant(conn, "GRANT EXECUTE ON ALL ROUTINES IN SCHEMA public TO {}", gateway)
    for table in CHECKPOINT_TABLES:
        _grant(conn, "GRANT MAINTAIN ON {} TO {}", table, gateway)
    default = "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT "
    _grant(conn, default + "SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}", owner, gateway)
    _grant(conn, default + "USAGE, SELECT, UPDATE ON SEQUENCES TO {}", owner, gateway)
    _grant(conn, default + "EXECUTE ON ROUTINES TO {}", owner, gateway)


def _grant_runner(conn: Conn, owner: str, runner: str) -> None:
    _grant(conn, "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}", runner)
    # Sequence USAGE lets runner INSERTs draw BIGSERIAL ids; table grants do not.
    _grant(conn, "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}", runner)
    default = "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT "
    _grant(conn, default + "SELECT ON TABLES TO {}", owner, runner)
    _grant(conn, default + "USAGE, SELECT ON SEQUENCES TO {}", owner, runner)
    for privileges, tables in _RUNNER_TABLE_GRANTS:
        for table in tables:
            _grant(conn, f"GRANT {privileges} ON {{}} TO {{}}", table, runner)
    _grant(conn, "GRANT USAGE, SELECT ON SEQUENCE agent_shell_ttl_renewals_id_seq TO {}", runner)
    grant_manifest_runner_access(conn, runner)


def apply_group_grants(conn: Conn, *, owner: str, database: str, groups: Groups) -> None:
    """Converge both groups' grant surface in the connected cluster database.

    Idempotent; grants are only added. The checkpoint tables and every table the
    runner matrix names must exist, so a missing table fails loudly instead of
    silently narrowing the contract.
    """
    require_admin(conn)
    row = conn.execute("SELECT current_database()").fetchone()
    if row is None or row[0] != database:
        raise AuthorityRefusedError("group grants must run connected to the cluster database")
    with conn.transaction():
        _grant_common(conn, database, groups)
        _grant_gateway(conn, owner, groups.gateway)
        _grant_runner(conn, owner, groups.runner)


def ensure_groups(conn: Conn, *, owner: str, database: str, groups: Groups) -> None:
    """Create missing NOLOGIN groups and converge their grants in one transaction.

    Never changes LOGIN: a group that can log in, holds elevated attributes,
    owns objects, carries role settings or is a member of another role is an
    unknown state and refuses. A legacy LOGIN ``ava_runner`` is demoted only by
    ``retire_legacy_logins`` under birth or cutover authority.
    """
    require_admin(conn)
    with conn.transaction():
        violations = group_violations(conn, groups)
        if violations:
            raise CatalogRefusedError(tuple(violations))
        _create_missing(conn, groups)
        apply_group_grants(conn, owner=owner, database=database, groups=groups)


def retire_legacy_logins(
    conn: Conn, *, owner: str, groups: Groups, authority: BirthAuthority | CutoverAuthority
) -> tuple[str, ...]:
    """Demote the schema owner and existing groups to NOLOGIN without a password.

    Monotone: only removes LOGIN. Refuses when the owner is the initdb bootstrap
    superuser (it cannot become NOLOGIN safely) or a superuser at all. Returns
    the roles this call demoted; sessions they already hold are closed by the
    caller's closure proof.
    """
    del authority
    require_admin(conn)
    demoted: list[str] = []
    with conn.transaction():
        facts = role_facts(conn, (owner, groups.gateway, groups.runner))
        if owner not in facts:
            raise CatalogRefusedError((f"schema owner {owner} does not exist",))
        if facts[owner].oid == BOOTSTRAP_SUPERUSER_OID or facts[owner].superuser:
            raise CatalogRefusedError(
                (f"schema owner {owner} is a superuser and cannot be retired",)
            )
        for name, fact in sorted(facts.items()):
            if fact.login or fact.password is not None:
                _grant(conn, "ALTER ROLE {} NOLOGIN PASSWORD NULL", name)
                demoted.append(name)
    return tuple(demoted)


class VacuumSkippedError(RuntimeError):
    """VACUUM warned instead of maintaining the table (PostgreSQL 17 skips
    tables the session may not MAINTAIN with only a WARNING)."""


def vacuum_or_fail(conn: Conn, table: str) -> None:
    """``VACUUM (ANALYZE) table`` where any WARNING is a failure, not a skip.

    ``conn`` must be autocommit (VACUUM cannot run in a transaction block).
    """
    if not conn.autocommit:
        raise VacuumSkippedError("VACUUM requires an autocommit connection")
    warnings: list[str] = []

    def on_notice(diagnostic: Diagnostic) -> None:
        if diagnostic.severity_nonlocalized == "WARNING":
            warnings.append(diagnostic.message_primary or "")

    conn.add_notice_handler(on_notice)
    try:
        conn.execute(sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(table)))
    finally:
        conn.remove_notice_handler(on_notice)
    if warnings:
        raise VacuumSkippedError(f"VACUUM {table} did not run: {warnings}")
