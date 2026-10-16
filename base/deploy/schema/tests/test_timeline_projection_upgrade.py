from __future__ import annotations

from typing import LiteralString, cast

import psycopg
from psycopg import sql

from base.cluster.machine import MachineIdentity
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database

_TIMELINE_PROJECTION = "20261016T153550_run-timeline-range-projection"
_TIMELINE_TABLES = (
    "run_timeline_segments",
    "run_timeline_message_facts",
    "run_timeline_blocks",
)


def test_timeline_projection_upgrade_is_discovered_and_preserves_history(
    db_url: str,
    machine_identity: MachineIdentity,
) -> None:
    _ = db_url
    with throwaway_database("timeline_upgrade") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            for table in reversed(_TIMELINE_TABLES):
                conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(table)))
            conn.execute("DROP INDEX understanding_nodes_span_end_idx")
            conn.execute("INSERT INTO agents (id, label) VALUES (1, 'timeline upgrade')")
            conn.execute(
                "INSERT INTO inbound_messages (id, agent_id, kind, content) "
                "VALUES (1, 1, 'chat', 'retained history')"
            )
        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(conn, machine_name=machine_identity.name)
            assert _TIMELINE_PROJECTION in applied
            for table in _TIMELINE_TABLES:
                assert conn.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (table,)
            assert (
                conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE indexname IN "
                    "('run_timeline_blocks_start_idx', 'run_timeline_blocks_end_idx', "
                    "'run_timeline_message_start_idx', 'run_timeline_message_end_idx', "
                    "'understanding_nodes_span_end_idx')"
                ).rowcount
                == 5
            )
            assert conn.execute(
                "SELECT content, status FROM inbound_messages WHERE id = 1"
            ).fetchone() == ("retained history", "pending")
        with psycopg.connect(url) as conn:
            assert apply_pending_migrations(conn, machine_name=machine_identity.name) == []
