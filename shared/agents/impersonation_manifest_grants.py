"""Least-privilege runner grants for the manifest certification boundary."""

from __future__ import annotations

import psycopg
from psycopg import sql


def grant_manifest_runner_access(conn: psycopg.Connection, runner_role: str) -> None:
    """Converge the narrow manifest authority surface for one runner role."""
    role = sql.Identifier(runner_role)
    conn.execute(sql.SQL("REVOKE INSERT, UPDATE ON agent_impersonations FROM {}").format(role))
    conn.execute(
        sql.SQL(
            "GRANT INSERT (id,agent_id,source,machine,reason,status,ttl_seconds,expires_at,"
            "relay_provider,relay_thread_id,relay_codex_remote,relay_token_hash,"
            "relay_batch_window_seconds,name,executor_name,process_metadata,automatic,"
            "ack_window_seconds,max_delivery_attempts,event_delivery_protocol_version) "
            "ON agent_impersonations TO {}"
        ).format(role)
    )
    conn.execute(
        sql.SQL(
            "GRANT UPDATE (status,ttl_seconds,expires_at,rejection_reason,summary_inbound_id,summary,"
            "accepted_generation,accepted_owner,consent_version,activated_at,ended_at,"
            "plugin_delta,delta_version,applied_version,relay_token_hash,relay_heartbeat_at,"
            "relay_last_failure_at,relay_minted_at,relay_minted_generation,relay_minted_owner,"
            "events_cursor,events_next_read_at,handoff_document,handoff_path,handoff_applied_at,"
            "next_entry,event_delivery_pending_reason,start_message) ON agent_impersonations TO {}"
        ).format(role)
    )
    for table in (
        "agent_impersonation_entries",
        "agent_impersonation_event_participants",
        "agent_impersonation_event_participant_items",
    ):
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(sql.Identifier(table), role)
        )
    for table in (
        "agent_impersonation_event_expected_receipts",
        "agent_impersonation_event_expected_items",
    ):
        conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(table), role))
    conn.execute(sql.SQL("REVOKE ALL ON agent_impersonation_event_certifiers FROM {}").format(role))
    conn.execute(
        sql.SQL("REVOKE UPDATE ON agent_impersonation_event_participants FROM {}").format(role)
    )
    for signature in (
        "public.close_impersonation_event_manifest_admission(uuid)",
        "public.admit_impersonation_event_certifier(uuid,text)",
        "public.seal_impersonation_event_participant(uuid,text,text,text,bigint,text)",
        "public.freeze_impersonation_event_manifest(uuid,text,bigint,timestamp with time zone)",
        "public.record_impersonation_event_retention_loss(uuid,timestamp with time zone)",
        "public.record_impersonation_event_integrity_alert(uuid)",
        "public.certify_impersonation_event_delivery(uuid,text)",
    ):
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(signature), role))
