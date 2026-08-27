"""Restore one encrypted database artifact into throwaway Postgres and verify it.

The default target is the newest managed artifact in the local backup directory;
pass an explicit artifact path to exercise a different retained copy. The drill
never touches the live database. It decrypts (removing the legacy gzip layer
when the artifact predates the 2026-08-27 double-gzip removal), restores into a
native throwaway Postgres cluster, verifies the recovery-source tables and a
checkpoint reader sample, then removes every scratch file and the throwaway
cluster.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

from services import backup
from shared import checkpoint as checkpoint_reader
from shared.pg_tools import pg_tool, throwaway_postgres


@dataclass(frozen=True)
class RestoreReport:
    """Observed data-plane facts from one successful scratch restore."""

    agents: int
    checkpoint_blobs: int
    checkpoints: int
    checkpoint_writes: int
    sample_agent_id: int
    sample_message_count: int
    agents_owner: str


def _newest_artifact() -> Path:
    artifacts = backup._managed_dumps(backup.backup_dir())
    if not artifacts:
        raise RuntimeError("no managed backup artifact exists")
    return artifacts[-1][1]


_RESTORE_ROLES = ("ava_main", "ava_runner", "grafana_ro")
"""Roles a managed dump's OWNER/GRANT statements reference. initdb only
creates the `ava` superuser; without these pg_restore fails on
`role "..." does not exist` (2026-08-27 prod drill finding). Attributes match
the live cluster's pg_roles: plain LOGIN roles, no password (trust auth)."""


def _ensure_restore_roles(db_url: str) -> None:
    """Create the dump-referenced roles in the throwaway cluster, idempotently."""
    from psycopg import sql as pgsql

    with psycopg.connect(db_url, autocommit=True) as conn:
        for role in _RESTORE_ROLES:
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if exists is None:
                conn.execute(pgsql.SQL("CREATE ROLE {} LOGIN").format(pgsql.Identifier(role)))


def _restore(raw_dump: Path, db_url: str) -> None:
    """Load the custom dump into the disposable target database."""
    proc = subprocess.run(  # noqa: S603
        [
            str(pg_tool("pg_restore")),
            "--clean",
            "--if-exists",
            "--dbname",
            db_url,
            str(raw_dump),
        ],
        capture_output=True,
        check=False,
        timeout=backup._DUMP_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_restore exited {proc.returncode}")


def verify_restored_database(db_url: str) -> RestoreReport:
    """Verify schema, table counts, and a readable checkpoint conversation.

    The sample thread is the time-newest one (`checkpoint->>'ts'`): ordering
    by `checkpoint_id` is textual, and non-UUID test rows sort above real
    UUIDs, which picked a May test residue over a live conversation."""

    required_tables = ("agents", "checkpoint_blobs", "checkpoints", "checkpoint_writes")
    with psycopg.connect(db_url, autocommit=True) as conn:
        for table in required_tables:
            row = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()
            if row is None or row[0] != table:
                raise RuntimeError(f"restored schema is missing {table}")
        agents = conn.execute("SELECT count(*) FROM agents").fetchone()
        blobs = conn.execute("SELECT count(*) FROM checkpoint_blobs").fetchone()
        checkpoints = conn.execute("SELECT count(*) FROM checkpoints").fetchone()
        writes = conn.execute("SELECT count(*) FROM checkpoint_writes").fetchone()
        sample = conn.execute(
            "SELECT thread_id FROM checkpoints ORDER BY checkpoint->>'ts' DESC NULLS LAST LIMIT 1"
        ).fetchone()
        owner = conn.execute(
            "SELECT pg_catalog.pg_get_userbyid(relowner) FROM pg_catalog.pg_class "
            "WHERE relname = 'agents' AND relnamespace = to_regnamespace('public')"
        ).fetchone()
    if agents is None or blobs is None or checkpoints is None or writes is None:
        raise RuntimeError("restored count query returned no row")
    if sample is None or not str(sample[0]).isdigit():
        raise RuntimeError("restored checkpoints contain no readable agent conversation")

    sample_agent_id = int(sample[0])
    original_url = checkpoint_reader.settings.data_plane.db_url
    checkpoint_reader.settings.data_plane.db_url = db_url
    try:
        messages = checkpoint_reader.load_checkpoint_messages_full(sample_agent_id)
    finally:
        checkpoint_reader.settings.data_plane.db_url = original_url
    if not messages:
        raise RuntimeError("restored checkpoint conversation has no messages")

    if owner is None or not owner[0]:
        raise RuntimeError("restored agents table has no resolvable owner")

    return RestoreReport(
        agents=agents[0],
        checkpoint_blobs=blobs[0],
        checkpoints=checkpoints[0],
        checkpoint_writes=writes[0],
        sample_agent_id=sample_agent_id,
        sample_message_count=len(messages),
        agents_owner=str(owner[0]),
    )


def run_drill(artifact: Path | None = None) -> tuple[RestoreReport, float]:
    """Run the complete decrypt, restore, and verification drill."""
    artifact = artifact or _newest_artifact()
    if not artifact.is_file():
        raise RuntimeError(f"backup artifact does not exist: {artifact.name}")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ava-restore-drill-") as tmp:
        scratch = Path(tmp)
        raw_dump = scratch / "backup.dump"
        backup.decrypt_artifact(artifact, raw_dump)
        # Legacy artifacts carry a gzip layer; current ones are raw archives.
        backup.gunzip_if_needed(raw_dump)
        with throwaway_postgres() as scratch_db_url:
            _ensure_restore_roles(scratch_db_url)
            _restore(raw_dump, scratch_db_url)
            report = verify_restored_database(scratch_db_url)
    return report, time.monotonic() - started


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore and verify an encrypted Ava DB backup.")
    parser.add_argument("artifact", nargs="?", type=Path, help="managed .dump.enc artifact")
    args = parser.parse_args()
    report, elapsed = run_drill(args.artifact)
    print(
        "restore drill passed: "
        f"agents={report.agents} checkpoints={report.checkpoints} "
        f"checkpoint_blobs={report.checkpoint_blobs} "
        f"checkpoint_writes={report.checkpoint_writes} "
        f"sample_agent={report.sample_agent_id} messages={report.sample_message_count} "
        f"agents_owner={report.agents_owner} "
        f"elapsed_seconds={elapsed:.1f}"
    )


if __name__ == "__main__":
    main()
