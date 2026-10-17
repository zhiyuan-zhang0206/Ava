# meiva:ref base/cluster/authority/docs/group-grants.meiva.okf.md#capability-grant-convergence
from __future__ import annotations

from typing import LiteralString

from psycopg import sql
from psycopg.errors import Diagnostic

from base.cluster.authority.catalog import (
    Conn,
    memberships,
    require_admin,
    role_facts,
)
from base.cluster.authority.event_grants import grant_event_log_runner_access
from base.host.db_authority.model import AuthorityRefusedError, CatalogRefusedError, Groups

CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


GATEWAY_APPEND_ONLY_TABLES = ("audit_events", "telemetry_events")


_RUNNER_TABLE_GRANTS: tuple[tuple[LiteralString, tuple[str, ...]], ...] = (
    ("SELECT, UPDATE", ("inbound_messages", "agents_meta")),
    ("INSERT, UPDATE", ("native_graph_work", "upload_delivery_copies", "native_restart_commands")),
    ("UPDATE", ("native_cancel_commands",)),
    ("INSERT", ("inbound_messages",)),
    ("INSERT", ("native_compact_observations",)),
    ("UPDATE", ("native_compact_commands",)),
    ("INSERT, UPDATE, SELECT", ("machine_units",)),
    ("INSERT, UPDATE", ("machines", "host_deploy_state")),
    ("INSERT, UPDATE, DELETE", ("api_idempotency",)),
    ("INSERT, UPDATE", ("agent_tasks", "agent_impersonation_messages")),
    ("INSERT", ("task_creation_receipts", "task_update_receipts")),
    ("SELECT, INSERT, UPDATE, DELETE", ("understanding_nodes",)),
    (
        "SELECT, INSERT, DELETE",
        ("run_timeline_segments", "run_timeline_message_facts", "run_timeline_blocks"),
    ),
    ("SELECT, INSERT, UPDATE", ("understanding_chunk_jobs",)),
    ("INSERT", ("understanding_chunk_calls",)),
    ("SELECT, INSERT, UPDATE, DELETE", ("understanding_group_state",)),
    ("INSERT", ("understanding_group_calls",)),
    ("SELECT, UPDATE", ("understanding_rebuilds",)),
    ("UPDATE", ("agent_pages",)),
    ("INSERT, UPDATE", ("agent_shell_ttls",)),
    ("SELECT, INSERT", ("agent_shell_ttl_renewals",)),
    ("SELECT, INSERT", ("heartbeat_pause_log",)),
    ("SELECT, INSERT", ("audit_events",)),
    ("SELECT, INSERT", ("telemetry_events",)),
    ("SELECT, INSERT, UPDATE", ("plugin_stats",)),
    ("SELECT, INSERT", ("agent_metric_observations",)),
    (
        "SELECT, INSERT, UPDATE",
        (
            "agent_metric_days",
            "agent_lifecycle_intervals",
            "agent_metric_file_cursors",
        ),
    ),
    ("ALL", CHECKPOINT_TABLES),
)


def group_violations(conn: Conn, groups: Groups) -> list[str]:
    names = (groups.gateway, groups.runner)
    facts = role_facts(conn, names)
    violations: list[str] = []
    for name, fact in facts.items():
        if fact.login or fact.password is not None:
            violations.append(f"group {name} can log in")
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
    for table in GATEWAY_APPEND_ONLY_TABLES:
        _grant(conn, "REVOKE UPDATE, DELETE ON {} FROM {}", table, gateway)
    default = "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT "
    _grant(conn, default + "SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}", owner, gateway)
    _grant(conn, default + "USAGE, SELECT, UPDATE ON SEQUENCES TO {}", owner, gateway)
    _grant(conn, default + "EXECUTE ON ROUTINES TO {}", owner, gateway)


def _grant_runner(conn: Conn, owner: str, runner: str) -> None:
    _grant(conn, "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}", runner)

    _grant(conn, "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}", runner)
    default = "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT "
    _grant(conn, default + "SELECT ON TABLES TO {}", owner, runner)
    _grant(conn, default + "USAGE, SELECT ON SEQUENCES TO {}", owner, runner)
    for privileges, tables in _RUNNER_TABLE_GRANTS:
        for table in tables:
            _grant(conn, f"GRANT {privileges} ON {{}} TO {{}}", table, runner)
    _grant(conn, "GRANT USAGE, SELECT ON SEQUENCE agent_shell_ttl_renewals_id_seq TO {}", runner)

    _grant(
        conn,
        "GRANT EXECUTE ON FUNCTION public.ensure_telemetry_event_partitions(integer, integer) TO {}",
        runner,
    )
    grant_event_log_runner_access(conn, runner)


def apply_group_grants(conn: Conn, *, owner: str, database: str, groups: Groups) -> None:
    require_admin(conn)
    row = conn.execute("SELECT current_database()").fetchone()
    if row is None or row[0] != database:
        raise AuthorityRefusedError("group grants must run connected to the cluster database")
    with conn.transaction():
        _grant_common(conn, database, groups)
        _grant_gateway(conn, owner, groups.gateway)
        _grant_runner(conn, owner, groups.runner)


def ensure_groups(conn: Conn, *, owner: str, database: str, groups: Groups) -> None:
    require_admin(conn)
    with conn.transaction():
        violations = group_violations(conn, groups)
        if violations:
            raise CatalogRefusedError(tuple(violations))
        _create_missing(conn, groups)
        apply_group_grants(conn, owner=owner, database=database, groups=groups)


class VacuumSkippedError(RuntimeError):
    pass


def vacuum_or_fail(conn: Conn, table: str) -> None:
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
