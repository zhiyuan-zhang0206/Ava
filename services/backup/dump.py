"""Daily local Postgres backup, driven by the gateway scheduler daemon.

One `pg_dump --format=custom` per day into `$AVA_HOME/backups/db/`,
keeping the newest ``backup_keep`` dumps (``services.backup_keep``, default 7).
`services.backup.scheduler.daemon` calls `is_due()` independently of watchdog
rounds: at the first wake after ``backup_hour`` cluster time with no dump for
the current cluster day, so a host that was down at 03:00 catches up. Its
operation worker runs `run_backup(staging=...)` inside private controls; the
controller links that artifact into the backup directory after the worker's
group closed (`services.backup.scheduler.worker`).

Backup cadence follows the configured cluster timezone; artifact names carry
UTC timestamps. Retention orders those timestamps independently of host DST.

Local dumps guard against bad migrations / accidental deletes / DB
corruption. `run_backup` is `dump -> encrypt -> optional off-site publish ->
prune`. The best-effort off-site leg publishes the encrypted artifact to OSS
iff absent (`services.backup.artifact.offsite`); an unconfigured,
unavailable or failing store keeps the local artifact, and nothing here deletes
a remote object (see `future/infra/data/pg-backup.md`). The dump uses PostgreSQL's
compressed custom format; legacy gzip artifacts stay restorable
(`gunzip_if_needed`).

The LangGraph checkpoint tables (`checkpoint_blobs`, `checkpoints`, and
`checkpoint_writes`) are the only copy of conversation history: messages, tool
outputs, and compaction segments all live there. Every daily dump includes them.
The custom dump is encrypted before publication, so local and optional off-site
artifacts contain the complete recoverable database without storing plaintext
conversation data at rest.

Only files matching `services.backup.artifact.names` are managed (counted
for due-ness, pruned); a hand-made dump parked in the same directory is never
touched.

Restore procedure: `.agents/skills/ava-guide/operations/references/db-restore.md`.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from base.clock import Clock
from base.cluster.dataplane.pg_tools import pg_tool
from base.db import Database, connect_url
from base.db.pg_admin import local_owner_authority
from base.host.private_storage import ensure_private_dir, ensure_private_file
from base.native_process.os_platform import LockTimeoutError, file_lock
from base.paths import ava_home
from services.backup.artifact import passphrase as backup_passphrase
from services.backup.artifact.intermediates import sweep_closed_partials
from services.backup.artifact.names import DUMP_NAME_RE, REMOTE_ROOT, TS_FORMAT, stamp_utc

_log = logging.getLogger(__name__)

# Headroom against a stall, not an expected runtime: a full dump with
# checkpoint history takes about 6.3 min.
_DUMP_TIMEOUT_S = 60 * 60
# Heartbeat cadence while a dump or an encryption runs with a progress sink
# attached. Either may run for many minutes without writing anything; a beat
# every 60 s keeps the sink's operator's view alive instead of reading a healthy
# dump as a hung one.
_PROGRESS_INTERVAL_S = 60.0
# Bound the composition-sample connection (a dead DB must stall the backup log
# line only this long before degrading to "unavailable", never hang it).
_BREAKDOWN_CONNECT_TIMEOUT_S = 10
_TARGET_NAME_ATTEMPTS = 60
_backup_lock_guard = threading.RLock()
_backup_lock_state = threading.local()


def _cluster_tz(*, clock_factory: Callable[[], Clock]) -> ZoneInfo:
    """The cluster wall clock every scheduling decision here is made in."""
    return clock_factory().explicit_zone()


def _require_aware(now: datetime) -> datetime:
    """A naive datetime is rejected rather than read as host-local: silently
    adopting the host's timezone is the exact failure this module was carrying."""
    if now.tzinfo is None:
        raise ValueError(f"backup needs a TZ-aware datetime, got naive {now!r}")
    return now


def backup_dir() -> Path:
    # `<home>/backups/db`: the home itself already scopes the cluster (path-only
    # identity), so the dump dir needs no per-cluster token. Pre-cutover dumps
    # under `backups/<cluster-name>` are left in place (at most ``backup_keep`` of
    # them); rotation continues in the new dir.
    return ava_home() / "backups" / "db"


@contextmanager
def backup_lock(*, timeout_s: float | None = None) -> Generator[None]:
    """Serialize backup creation and verification across local processes.

    The lock is re-entrant within one thread, so a caller holding it can call
    `run_backup`.
    """
    with _backup_lock_guard:
        depth = getattr(_backup_lock_state, "depth", 0)
        if depth:
            _backup_lock_state.depth = depth + 1
            try:
                yield
            finally:
                _backup_lock_state.depth -= 1
            return

        lock_path = backup_dir().parent / ".db-backup.lock"
        with file_lock(lock_path, timeout_s=timeout_s):
            _backup_lock_state.depth = 1
            try:
                yield
            finally:
                del _backup_lock_state.depth


def _managed_dumps(directory: Path) -> list[tuple[datetime, Path]]:
    """This module's dumps in `directory`, oldest first, keyed by UTC instant."""
    dumps: list[tuple[datetime, Path]] = []
    if not directory.exists():
        return dumps
    for path in directory.iterdir():
        m = DUMP_NAME_RE.match(path.name)
        if m and path.is_file():
            dumps.append((stamp_utc(m["ts"]), path))
    return sorted(dumps)


def is_due(
    now: datetime, *, clock_factory: Callable[[], Clock], hour_reader: Callable[[], int]
) -> bool:
    """True once the cluster clock has passed ``backup_hour`` with no dump for the
    current cluster day. `now` must be TZ-aware."""
    local_now = _require_aware(now).astimezone(_cluster_tz(clock_factory=clock_factory))
    if local_now.hour < hour_reader():
        return False
    dumps = _managed_dumps(backup_dir())
    tz = _cluster_tz(clock_factory=clock_factory)
    return not dumps or dumps[-1][0].astimezone(tz).date() < local_now.date()


def _prune(directory: Path, *, keep_reader: Callable[[], int]) -> list[Path]:
    """Delete managed dumps beyond retention: all but the newest ``backup_keep``."""
    dumps = _managed_dumps(directory)
    keep = set(dumps[-keep_reader() :])
    removed: list[Path] = []
    for item in dumps:
        if item not in keep:
            item[1].unlink()
            removed.append(item[1])
    return removed


def dump_source(db: Database, *, is_remote_reader: Callable[[], bool]) -> str:
    """The dial `pg_dump` reads this cluster's whole database through.

    A locally owned plane dumps as the administrator acting as the schema owner
    over the home's owner-only socket (`base.db.pg_admin`): password-free,
    custody-checked against this home's postmaster, and independent of the
    write generation, so a dump never needs, and never dies with, a delivered
    login. A remote-managed plane's provider URL
    (`Database.direct_url`) is its only authority; `_passwordless_conninfo` keeps its
    password off argv. Both bypass PgBouncer: pg_dump holds one snapshot across
    many statements, which a transaction pooler cannot keep.
    """
    if is_remote_reader():
        return db.direct_url()
    return local_owner_authority().verified_conninfo()


def _passwordless_conninfo(db_url: str) -> tuple[str, str]:
    """Return pg_dump conninfo and password separately so the latter never enters argv."""
    parsed = conninfo_to_dict(db_url)
    # Preserve SSL and other connection settings.  Password is the sole field
    # deliberately split into the child-only environment below.
    fields = {key: str(value) for key, value in parsed.items() if key != "password"}
    conninfo = make_conninfo(**fields)
    password = parsed.get("password")
    return conninfo, password if isinstance(password, str) else ""


def _key_file(directory: Path, *, legacy_empty_secret: bool = False) -> Path:
    """A private temporary key file holding the pinned logical-backup passphrase."""
    return backup_passphrase.write_key_file(directory, legacy_empty_secret=legacy_empty_secret)


def decrypt_artifact(
    artifact: Path, custom_dump: Path, *, legacy_empty_secret: bool = False
) -> None:
    """Decrypt one managed artifact into a custom-format dump.

    `custom_dump` is the raw `pg_dump --format=custom` archive for artifacts
    written by the current pipeline; for legacy `<db>-<ts>.dump.gz.enc`
    artifacts it is the gzip-compressed archive (call `gunzip_if_needed`).
    The caller owns `custom_dump` and removes it once consumed. The pinned
    passphrase decrypts, never placed on argv; `legacy_empty_secret` (an
    explicit restore option, never a fallback) uses the public pre-cutover key
    of an empty-secret home instead.
    """
    custom_dump.touch(mode=0o600, exist_ok=False)
    custom_dump.chmod(0o600)
    key_file = _key_file(custom_dump.parent, legacy_empty_secret=legacy_empty_secret)
    try:
        proc = subprocess.run(  # noqa: S603
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-kfile",
                str(key_file),
                "-in",
                str(artifact),
                "-out",
                str(custom_dump),
            ],
            capture_output=True,
            check=False,
            timeout=_DUMP_TIMEOUT_S,
        )
        if proc.returncode != 0:
            hint = "" if legacy_empty_secret else f"; {backup_passphrase.LEGACY_RESTORE_HINT}"
            raise RuntimeError(f"backup decrypt exited {proc.returncode}{hint}")
    finally:
        with suppress(OSError):
            key_file.unlink(missing_ok=True)


def gunzip_if_needed(path: Path, *, timeout_s: float = _DUMP_TIMEOUT_S) -> None:
    """Decompress `path` in place when it is a gzip stream, else leave it alone.

    The current pipeline publishes raw custom-format dumps (`.dump.enc`), but
    legacy artifacts (`.dump.gz.enc`, written before the double-gzip removal)
    carry a gzip layer around the archive. Restore paths call this so every
    managed artifact stays restorable through one procedure during and after
    the transition; the gzip magic header (``1f 8b``) decides.
    """
    with path.open("rb") as handle:
        magic = handle.read(2)
    if magic != b"\x1f\x8b":
        return
    decompressed = path.with_name(path.name + ".raw")
    try:
        # 0600 like every other decrypted intermediate: the plaintext dump must
        # not widen to umask (typically 0644) when the archive is replaced.
        fd = os.open(decompressed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            proc = subprocess.run(  # noqa: S603
                ["gzip", "--decompress", "--stdout", str(path)],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_s,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"backup gunzip exited {proc.returncode}")
        decompressed.replace(path)
    finally:
        with suppress(OSError):
            decompressed.unlink(missing_ok=True)


def _available_target(directory: Path, dbname: str, now: datetime) -> Path:
    """Return an unused managed dump path without replacing a prior dump."""
    for offset_s in range(_TARGET_NAME_ATTEMPTS):
        stamp = (now + timedelta(seconds=offset_s)).astimezone(UTC).strftime(TS_FORMAT)
        target = directory / f"{dbname}-{stamp}.dump.enc"
        if not target.exists():
            return target
    raise RuntimeError("could not choose a distinct backup filename within 60 seconds")


# A one-line progress report, called at most every `_PROGRESS_INTERVAL_S` while a
# long, otherwise-silent pipeline stage runs (see `_run_with_progress`).
_ProgressSink = Callable[[str], None]


def _written_suffix(size_path: Path | None) -> str:
    """`, N MiB written` for a child's output file — "" when it cannot be read."""
    if size_path is None:
        return ""
    try:
        written = size_path.stat().st_size
    except OSError:
        return ""
    return f", {written / 2**20:.1f} MiB written"


def _run_with_progress(
    argv: list[str],
    *,
    timeout_s: float,
    label: str,
    progress: _ProgressSink | None,
    env: dict[str, str] | None = None,
    size_path: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """`subprocess.run(argv, capture_output=True, check=False)` that narrates its wait.

    With `progress=None` this is exactly `subprocess.run`. With a sink, one line
    names the child's bound and one every `_PROGRESS_INTERVAL_S` carries the
    elapsed time and the bytes `size_path` holds, so a caller's operator sees a
    long silent dump or encryption alive until its own `timeout_s`.

    Like `subprocess.run`, expiry or any interruption kills and reaps the child
    before raising, so its output never has a live writer afterwards.
    """
    if progress is None:
        return subprocess.run(  # noqa: S603
            argv, capture_output=True, check=False, env=env, timeout=timeout_s
        )
    started = time.monotonic()
    progress(f"{label} started (bounded at {timeout_s / 60:.0f} min)")
    with subprocess.Popen(  # noqa: S603
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    ) as proc:
        try:
            stdout, stderr = _narrated_wait(
                proc, argv, timeout_s, started, label, progress, size_path
            )
        except BaseException:
            proc.kill()
            raise  # `Popen.__exit__` reaps it.
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _narrated_wait(
    proc: subprocess.Popen[bytes],
    argv: list[str],
    timeout_s: float,
    started: float,
    label: str,
    progress: _ProgressSink,
    size_path: Path | None,
) -> tuple[bytes, bytes]:
    while True:
        remaining_s = timeout_s - (time.monotonic() - started)
        if remaining_s <= 0:
            raise subprocess.TimeoutExpired(argv, timeout_s)
        try:
            return proc.communicate(timeout=min(_PROGRESS_INTERVAL_S, remaining_s))
        except subprocess.TimeoutExpired:
            progress(f"{label} {time.monotonic() - started:.0f}s{_written_suffix(size_path)}")


def run_backup(
    now: datetime | None = None,
    *,
    db: Database,
    is_remote_reader: Callable[[], bool],
    db_url: str | None = None,
    timeout_s: float = _DUMP_TIMEOUT_S,
    publish: bool = True,
    progress: _ProgressSink | None = None,
    staging: Path | None = None,
    keep_reader: Callable[[], int],
    endpoint_reader: Callable[[], str],
    bucket_reader: Callable[[], str],
    credentials_file_reader: Callable[[], Path | None],
) -> Path:
    """Dump the cluster DB into backup_dir() and prune; return the dump path.

    Plaintext and encrypted intermediates use `.partial` names; only the
    encrypted custom-format artifact is published after every pipeline step
    succeeds. A failed step removes its intermediates once its tool is reaped.
    `staging` writes into a scheduled operation's private controls instead
    and leaves publication and pruning to that operation's controller.

    `timeout_s` lets bounded callers use a tighter ceiling than the daily
    backup default. `publish=False` keeps the completed artifact local, so
    off-site network latency cannot extend the run.

    `progress` narrates the stages that may run for minutes without writing
    anything (`pg_dump` and the encryption pass) — see `_run_with_progress`.
    """
    with backup_lock():
        directory = backup_dir() if staging is None else staging
        # Every run, the scheduled worker's staged one included, first clears
        # the backup directory of intermediates whose writers closed: a killed
        # in-process snapshot's plaintext never waits for another snapshot.
        for swept in dict.fromkeys((backup_dir(), directory)):
            sweep_closed_partials(ensure_private_dir(swept))
        target = _run_backup(
            now,
            db=db,
            is_remote_reader=is_remote_reader,
            directory=directory,
            db_url=db_url,
            timeout_s=timeout_s,
            progress=progress,
        )
        if publish:
            # Imported where used: the scheduler daemon imports this module for
            # `is_due` and must not carry the OSS SDK; its backup worker, which
            # reaches this line, loads it.
            from services.backup.artifact import offsite

            offsite.publish(
                target,
                endpoint_reader=endpoint_reader,
                bucket_reader=bucket_reader,
                credentials_file_reader=credentials_file_reader,
            )
        if staging is None:
            _log_written(target, _prune(target.parent, keep_reader=keep_reader))
        return target


def prune_after_publish(target: Path, *, keep_reader: Callable[[], int]) -> None:
    """Prune around a newly linked scheduled dump without waiting on the lock.

    Linking never replaces a managed name, so it needs no lock; pruning does.
    A busy lock defers pruning to the next backup instead of stalling the
    scheduler.
    """
    try:
        with backup_lock(timeout_s=0):
            removed = _prune(target.parent, keep_reader=keep_reader)
    except LockTimeoutError:
        _log.info("[backup] prune deferred while another backup owns the lock")
        removed = []
    _log_written(target, removed)


def _db_size_breakdown(db: Database, db_url: str | None = None) -> str:
    """One-line DB composition for the backup log: total, the LangGraph
    checkpoint tables, and everything else.

    The checkpoint tables dominate DB size and dump time, so each artifact
    carries its own growth baseline. Best-effort: a failure (e.g. a fresh
    cluster missing the tables) degrades to "unavailable" and never fails the
    backup. `db_url` is the same database the dump reads; None falls back to
    the settings-derived direct connection.
    """
    try:
        with (
            connect_url(db_url, autocommit=True, connect_timeout=_BREAKDOWN_CONNECT_TIMEOUT_S)
            if db_url is not None
            else db.connect(direct=True, autocommit=True)
        ) as conn:
            row = conn.execute(
                """
                SELECT pg_database_size(current_database()),
                       COALESCE(pg_total_relation_size(to_regclass('public.checkpoint_blobs')), 0),
                       COALESCE(pg_total_relation_size(to_regclass('public.checkpoints')), 0),
                       COALESCE(pg_total_relation_size(to_regclass('public.checkpoint_writes')), 0)
                """
            ).fetchone()
    except psycopg.Error:
        _log.warning("[backup] db size breakdown unavailable", exc_info=True)
        return "unavailable"
    assert row is not None  # noqa: S101 — aggregate over fixed tables always returns one row
    total, blobs, checkpoints, writes = (int(v) for v in row)
    checkpoint = blobs + checkpoints + writes
    rest = max(total - checkpoint, 0)
    return f"db={_mb(total)}MiB checkpoint={_mb(checkpoint)}MiB rest={_mb(rest)}MiB"


def _mb(b: int) -> int:
    return round(b / 2**20)


def _log_written(target: Path, removed: list[Path]) -> None:
    _log.info(
        "[backup] wrote %s (%.1f MiB), pruned %d",
        target,
        target.stat().st_size / 2**20,
        len(removed),
    )


def _run_backup(
    now: datetime | None = None,
    *,
    db: Database,
    is_remote_reader: Callable[[], bool],
    db_url: str | None = None,
    timeout_s: float = _DUMP_TIMEOUT_S,
    directory: Path,
    progress: _ProgressSink | None = None,
) -> Path:
    """Write one managed dump while `backup_lock` is held.

    Pipeline: `pg_dump --format=custom --compress=zstd:3` (the custom archive
    compresses in-dump; there is no separate gzip stage), then AES-CBC
    encryption. `timeout_s` bounds every subprocess; the caller owns the lock.

    `progress` narrates the two stages that may run for minutes without writing
    anything: `pg_dump` and the encryption pass (see `_run_with_progress`).
    """
    now = _require_aware(now) if now is not None else datetime.now(UTC)
    db_url = db_url if db_url is not None else dump_source(db, is_remote_reader=is_remote_reader)
    directory = ensure_private_dir(directory)
    db_conninfo, password = _passwordless_conninfo(db_url)
    dbname = cast(str, conninfo_to_dict(db_url)["dbname"])
    _log.info("[backup] db composition: %s", _db_size_breakdown(db, db_url))
    target = _available_target(directory, dbname, now)
    stem = target.name.removesuffix(".dump.enc")
    dump_partial = directory / f"{stem}.dump.partial"
    encrypted_partial = target.with_name(target.name + ".partial")
    dump_partial.touch(mode=0o600, exist_ok=False)
    try:
        _dump_and_encrypt(
            db_conninfo, password, dump_partial, encrypted_partial, timeout_s, progress
        )
        encrypted_partial.rename(target)
    finally:
        # Every tool below is a reaped direct child once control returns here,
        # so its plaintext output has no live writer and never outlives the run.
        for partial in (dump_partial, encrypted_partial):
            partial.unlink(missing_ok=True)
    ensure_private_file(target)
    return target


def _dump_and_encrypt(
    db_conninfo: str,
    password: str,
    dump_partial: Path,
    encrypted_partial: Path,
    timeout_s: float,
    progress: _ProgressSink | None,
) -> None:
    dump_partial.chmod(0o600)
    # Pass only the credential this process owns to pg_dump. In particular, do
    # not inherit a shell's PGPASSWORD: a no-auth cluster must not accidentally
    # authenticate with another cluster's value (#550 alignment).
    dump_env = {"PGPASSWORD": password} if password else {}
    dump_cmd = [
        str(pg_tool("pg_dump")),
        "--format=custom",
        # The archive's own zstd compression (PG 17+). A second, external
        # compression pass used to gzip the already-compressed archive (13-47 s
        # benchmarked on the production DB for <1% size gain) — removed 2026-08-27.
        "--compress=zstd:3",
        "--file",
        str(dump_partial),
        "--dbname",
        db_conninfo,
    ]
    # The scheduler owns this subprocess in its own process, so its bound
    # cannot delay watchdog supervision. Expiry kills the child and
    # TimeoutExpired lets the scheduler schedule its retry.
    proc = _run_with_progress(
        dump_cmd,
        timeout_s=timeout_s,
        label="pg_dump",
        progress=progress,
        env=dump_env,
        size_path=dump_partial,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump exited {proc.returncode}")

    encrypted_partial.touch(mode=0o600, exist_ok=False)
    encrypted_partial.chmod(0o600)
    key_file = _key_file(dump_partial.parent)
    try:
        proc = _run_with_progress(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-kfile",
                str(key_file),
                "-in",
                str(dump_partial),
                "-out",
                str(encrypted_partial),
            ],
            timeout_s=timeout_s,
            label="backup encryption",
            progress=progress,
            size_path=encrypted_partial,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"backup encryption exited {proc.returncode}")
    finally:
        with suppress(OSError):
            key_file.unlink(missing_ok=True)


def _main(argv: list[str] | None = None) -> int:
    """Run the detached, best-effort off-site backup publisher."""
    # Standalone runs leave the module's INFO records to lastResort (WARNING+
    # only): without this the store-verified publish ACK never appears, so a
    # successful upload reads as a silent death (2026-09-16 misdiagnosis).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="python -m services.backup.dump")
    parser.add_argument("--publish-offsite", type=Path, metavar="ARTIFACT")
    parser.add_argument(
        "--offsite-root",
        default=REMOTE_ROOT,
        metavar="PREFIX",
        help=f"object-name prefix of the off-site namespace (default {REMOTE_ROOT})",
    )
    args = parser.parse_args(argv)
    if args.publish_offsite is None:
        parser.error("--publish-offsite is required")
    artifact = args.publish_offsite
    if not artifact.is_absolute():
        parser.error("--publish-offsite ARTIFACT must be an absolute path")
    if not args.offsite_root or args.offsite_root != args.offsite_root.strip("/"):
        parser.error("--offsite-root must be a non-empty prefix without surrounding slashes")
    from base.config import ConfigBoot
    from services.backup.artifact import offsite  # the SDK loads only on this entry

    config = ConfigBoot()
    config.boot()
    offsite.publish(
        artifact,
        root=args.offsite_root,
        endpoint_reader=lambda: config.view.services.backup_offsite_endpoint,
        bucket_reader=lambda: config.view.services.backup_offsite_bucket,
        credentials_file_reader=lambda: config.view.services.backup_offsite_credentials_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
