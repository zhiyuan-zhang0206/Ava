"""Crash-journaled archive and PITR environment application."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

from cli.commands._cluster_instance import pg_admin_url
from services.pitr.activation_runtime import (
    _enable_pitr_services,
    _file_evidence,
    _settings_digest,
    pitr_env_is_desired,
)
from services.pitr.activation_state import ActivationRecord, write_record_cas
from shared.cluster import get_record, record_postgres_port
from shared.paths import ava_home


def _pg_connection() -> psycopg.Connection[tuple[object, ...]]:
    cluster = get_record(ava_home())
    if cluster is None:
        raise RuntimeError("cluster registry record is missing")
    return psycopg.connect(pg_admin_url(record_postgres_port(cluster)), autocommit=True)


def _archive_value(name: str) -> str:
    with _pg_connection() as conn:
        row = conn.execute(sql.SQL("SHOW {}").format(sql.Identifier(name))).fetchone()
    if row is None:
        raise RuntimeError(f"PostgreSQL omitted {name}")
    return str(row[0])


def _alter(name: str, value: str) -> None:
    """ALTER SYSTEM rejects bound parameters ($1) — the value must ride as a
    quoted literal in the statement text (real PG17, 2026-08-30 activation)."""
    with _pg_connection() as conn:
        conn.execute(
            sql.SQL("ALTER SYSTEM SET {} = {}").format(sql.Identifier(name), sql.Literal(value))
        )


def _persistent_archive_settings(home: Path) -> dict[str, str]:
    path = home / "pg" / "postgresql.auto.conf"
    names = ("archive_mode", "archive_command", "archive_timeout", "wal_compression")
    result: dict[str, str] = {}
    with _pg_connection() as conn:
        for name in names:
            row = conn.execute(
                """SELECT setting FROM pg_file_settings
                   WHERE sourcefile = %s AND name = %s AND error IS NULL
                   ORDER BY sourceline DESC LIMIT 1""",
                (str(path), name),
            ).fetchone()
            result[name] = "__ABSENT__" if row is None else str(row[0])
    return result


def _alter_restore(name: str, value: str) -> None:
    with _pg_connection() as conn:
        query = (
            sql.SQL("ALTER SYSTEM RESET {}").format(sql.Identifier(name))
            if value == "__ABSENT__"
            else sql.SQL("ALTER SYSTEM SET {} = {}").format(
                sql.Identifier(name), sql.Literal(value)
            )
        )
        conn.execute(query)


def _journal_rollback(home: Path, record: ActivationRecord, **changes: object) -> ActivationRecord:
    replacement = record.journal_rollback(**changes)
    write_record_cas(home, expected=record, replacement=replacement)
    return replacement


def restore_archive_settings(
    home: Path, record: ActivationRecord, baseline: dict[str, str]
) -> ActivationRecord:
    """Restore each owned setting behind an intent/applied crash journal."""

    auto_path = home / "pg" / "postgresql.auto.conf"
    for name in ("archive_mode", "archive_command", "archive_timeout", "wal_compression"):
        desired = baseline[name]
        current = _persistent_archive_settings(home)[name]
        intent = record.rollback_setting_intent
        applied = dict(record.rollback_settings_applied or {})
        if name in applied:
            evidence = json.loads(applied[name])
            if current != evidence["desired_value"]:
                raise RuntimeError(f"PostgreSQL {name} changed after durable rollback apply")
            continue
        if intent is not None:
            if (
                intent.get("name") != name
                or current not in {intent.get("current_value"), desired}
                or intent.get("desired_value") != desired
            ):
                raise RuntimeError("PostgreSQL setting differs from durable rollback intent")
        elif current != desired:
            record = _journal_rollback(
                home,
                record,
                rollback_setting_intent={
                    "name": name,
                    "expected_digest": _file_evidence(auto_path)[1],
                    "current_value": current,
                    "desired_value": desired,
                },
            )
            intent = record.rollback_setting_intent
        if current != desired:
            if intent is None or current != intent["current_value"]:
                raise RuntimeError("PostgreSQL rollback setting has concurrent owned-field drift")
            _alter_restore(name, desired)
            current = _persistent_archive_settings(home)[name]
            if current != desired:
                raise RuntimeError(f"PostgreSQL {name} did not reach its rollback baseline")
        applied[name] = json.dumps(
            {
                "desired_value": desired,
                "post_digest": _file_evidence(auto_path)[1],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        record = _journal_rollback(
            home,
            record,
            rollback_setting_intent=None,
            rollback_settings_applied=applied,
            rollback_expected_auto_conf_digest=_file_evidence(auto_path)[1],
        )
    return record


def _journal(home: Path, record: ActivationRecord, **changes: object) -> ActivationRecord:
    replacement = record.journal_config(**changes)
    write_record_cas(home, expected=record, replacement=replacement)
    return replacement


def _env_payload(home: Path) -> bytes:
    return (home / ".env").read_bytes()


def apply_wal_config(
    home: Path, record: ActivationRecord, desired: dict[str, str]
) -> ActivationRecord:
    auto_path = home / "pg" / "postgresql.auto.conf"
    for name, value in desired.items():
        current = _archive_value(name)
        intent = record.config_apply_intent
        if current == value:
            if intent is not None and intent.get("name") == name:
                digest = _file_evidence(auto_path)[1]
                record = _journal(
                    home,
                    record,
                    config_apply_intent=None,
                    config_apply_applied={
                        "kind": "postgresql_auto_conf",
                        "name": name,
                        "digest": digest,
                    },
                    rollback_expected_auto_conf_digest=digest,
                )
            continue
        expected = _file_evidence(auto_path)[1]
        if intent is not None and (
            intent.get("kind") != "postgresql_auto_conf"
            or intent.get("name") != name
            or intent.get("expected_digest") != expected
            or intent.get("desired_value") != value
        ):
            raise RuntimeError("PostgreSQL config differs from durable apply intent")
        if intent is None:
            record = _journal(
                home,
                record,
                config_apply_intent={
                    "kind": "postgresql_auto_conf",
                    "name": name,
                    "expected_digest": expected,
                    "desired_value": value,
                },
            )
        _alter(name, value)
        digest = _file_evidence(auto_path)[1]
        record = _journal(
            home,
            record,
            config_apply_intent=None,
            config_apply_applied={"kind": "postgresql_auto_conf", "name": name, "digest": digest},
            rollback_expected_auto_conf_digest=digest,
        )

    payload = _env_payload(home)
    expected = hashlib.sha256(payload).hexdigest()
    intent = record.config_apply_intent
    if intent is None:
        record = _journal(
            home,
            record,
            config_apply_intent={
                "kind": "env",
                "expected_digest": expected,
                "desired_digest": _settings_digest(
                    {
                        "pitr_enabled": "true",
                        "pitr_base_backup_enabled": "true",
                        "pitr_restore_proof_enabled": "true",
                        "pitr_retention_planner_enabled": "false",
                    }
                ),
            },
        )
        intent = record.config_apply_intent
    if pitr_env_is_desired(payload):
        owned = payload
    else:
        if (
            intent is None
            or intent.get("kind") != "env"
            or intent.get("expected_digest") != expected
        ):
            raise RuntimeError(".env differs from durable PITR apply intent")
        owned = _enable_pitr_services(expected)
    env_digest = hashlib.sha256(owned).hexdigest()
    record = _journal(
        home,
        record,
        config_apply_intent=None,
        config_apply_applied={"kind": "env", "digest": env_digest},
        rollback_expected_env_digest=env_digest,
    )
    replacement = record.advance(
        "wal_restart_pending",
        restart_handoff=str(uuid.uuid4()),
        restart_orchestration=str(uuid.uuid4()),
        rollback_expected_auto_conf_digest=_file_evidence(auto_path)[1],
        error=None,
    )
    write_record_cas(home, expected=record, replacement=replacement)
    return replacement
