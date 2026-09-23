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
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

from services import backup
from shared.agents.history import checkpoint as checkpoint_reader
from shared.config import settings
from shared.log import logger
from shared.pg_throwaway_base import format_bytes, select_throwaway_base
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


_RESTORE_ROLES = ("ava_main", "ava_runner", "grafana_ro", "zzy")
"""Roles a managed dump's OWNER/GRANT statements reference. initdb only
creates the `ava` superuser; without these pg_restore fails on
`role "..." does not exist` (2026-08-27 prod drill finding). Attributes match
the live cluster's pg_roles: plain LOGIN roles, no password (trust auth).
`zzy` is the live admin role ad-hoc artifacts are created under (the
`model_sweep_backup_*` sweep convention); the dump re-owns such objects to it,
so the scratch cluster must carry the role too (2026-09-21 prod drill finding).
It is created as a plain role: a restore needs it only to exist for the OWNER
statements."""


def _ensure_restore_roles(db_url: str) -> None:
    """Create the dump-referenced roles in the throwaway cluster, idempotently."""
    from psycopg import sql as pgsql

    with psycopg.connect(db_url, autocommit=True) as conn:
        for role in _RESTORE_ROLES:
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if exists is None:
                conn.execute(pgsql.SQL("CREATE ROLE {} LOGIN").format(pgsql.Identifier(role)))


def _restore(raw_dump: Path, db_url: str, *, base: Path) -> None:
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
        raise RuntimeError(_restore_failure_message(proc, base))


def _restore_failure_message(proc: subprocess.CompletedProcess[bytes], base: Path) -> str:
    """The pg_restore failure with the capacity context an operator needs.

    A scratch restore that outgrows its base dies server-side mid-COPY, so the
    client-side failure carries no diagnosis of its own: the 2026-09-14 WSL run
    surfaced only `pg_restore exited 1` while the postmaster had been killed by
    the full tmpfs (dmesg signal 6; PQputCopyData: server closed the connection).
    Naming the base and its free space makes the next occurrence diagnosable from
    the drill's own output."""
    hint = (
        f"pg_restore exited {proc.returncode} against the throwaway cluster on {base} "
        f"({format_bytes(shutil.disk_usage(base).free)} free). If the server closed "
        f"the connection mid-copy, the scratch base ran out of room — free space on "
        f"{base} or point AVA_PG_THROWAWAY_BASE at a larger volume."
    )
    stderr_tail = "\n".join((proc.stderr or b"").decode(errors="replace").splitlines()[-5:])
    return f"{hint}\npg_restore stderr tail:\n{stderr_tail}" if stderr_tail else hint


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
    original_url = settings.data_plane.db_url
    settings.data_plane.db_url = db_url
    try:
        messages = checkpoint_reader.load_checkpoint_messages_full(sample_agent_id)
    finally:
        settings.data_plane.db_url = original_url
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


_SCRATCH_SPACE_FACTOR = 3.0
"""Scratch space the drill reserves on the base holding the restored copy, as a
multiple of the (decrypted) dump's size. A floor for picking the base, not a size
prediction: the restore holds the dump's content decompressed, and in-dump zstd
ratios vary with content mix. Measured on the WSL daily drill: 2026-09-14 restored
>=17 GiB from a 10.16 GiB artifact (~1.7x); 2026-09-19 restored ~8 GiB from a
~3.0 GiB artifact (>=2.7x at 60s sampling — it outgrew the 7.8 GiB tmpfs that 2x
had cleared, task #4033). 3x keeps margin above the observed maximum while
remaining a floor. A base that clears it is not guaranteed to fit; one that fails
it is almost certainly too small."""


def _scratch_space_requirement(raw_dump: Path) -> int:
    """Bytes the base holding this dump's restore must offer (an estimate — see
    `_SCRATCH_SPACE_FACTOR`)."""
    return int(raw_dump.stat().st_size * _SCRATCH_SPACE_FACTOR)


def run_drill(
    artifact: Path | None = None, *, foreground: bool = False
) -> tuple[RestoreReport, float]:
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
        required = _scratch_space_requirement(raw_dump)
        base = select_throwaway_base(required)
        logger.info(
            f"restore drill: scratch cluster on {base} "
            f"(estimate {format_bytes(required)} for artifact {artifact.name})"
        )
        with throwaway_postgres(base=base, foreground=foreground) as scratch_db_url:
            _ensure_restore_roles(scratch_db_url)
            _restore(raw_dump, scratch_db_url, base=base)
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
