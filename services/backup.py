"""Daily local Postgres backup, driven by the gateway scheduler daemon.

One `pg_dump --format=custom` per day into `$AVA_HOME/backups/db/`,
keeping the newest ``backup_keep`` dumps (``services.backup_keep``, default 7).
`services.backup_scheduler.daemon` calls `is_due()` independently of watchdog
rounds: at the first wake after ``backup_hour`` cluster time with no dump for
the current cluster day, so a host that was down at 03:00 catches up. Its
operation worker runs `run_backup(staging=...)` inside private controls; the
controller publishes that artifact after the worker's group closed
(`services.backup_scheduler.worker`). In-process snapshot callers use
`run_backup()` directly.

Backup cadence follows the configured cluster timezone; artifact names carry
UTC timestamps. Retention orders those timestamps independently of host DST.

Local dumps guard against bad migrations / accidental deletes / DB
corruption. `run_backup` is `dump -> encrypt -> optional off-site publish ->
prune`. The best-effort off-site leg publishes the encrypted artifact iff
absent through the shared backup store contract (`services.pitr.store_factory`,
the physical PITR plane's backend switch); a failed store keeps the local
artifact. Remote objects are append-only except policy-owned, armed retention
deletions (see `future/infra/pg-backup.md`). The dump uses PostgreSQL's
compressed custom format; legacy gzip artifacts stay restorable
(`gunzip_if_needed`).

The LangGraph checkpoint tables (`checkpoint_blobs`, `checkpoints`, and
`checkpoint_writes`) are the only copy of conversation history: messages, tool
outputs, and compaction segments all live there. Every daily dump includes them.
The custom dump is encrypted before publication, so local and optional off-site
artifacts contain the complete recoverable database without storing plaintext
conversation data at rest.

Only files matching this module's naming are managed (counted for due-ness,
pruned); a hand-made dump parked in the same directory is never touched. The
encrypted UTC names and legacy plaintext `<dbname>-YYYYMMDD-HHMMSS.dump` names
remain managed during the transition, so the old week of artifacts still prunes
instead of becoming stranded.

Restore procedure: `.agents/skills/operating-ava-cluster/references/db-restore.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from services.gateway_side.backup.intermediates import sweep_closed_partials
from services.pitr.logical_dump_names import (
    ACTIVATION_MARKER,
    DUMP_NAME_RE,
    PRE_UPDATE_MARKER,
    REMOTE_ROOT,
    TS_FORMAT,
    stamp_utc,
)
from shared.config import settings
from shared.db import connect, direct_db_url
from shared.pg_admin import local_owner_authority
from shared.pg_tools import pg_tool
from shared.platform import LockTimeoutError, file_lock
from shared.private_storage import ensure_private_dir, ensure_private_file

_log = logging.getLogger(__name__)

# Newest activation snapshots kept in their own prune slot: the current PITR
# activation's logical floor plus the one before it; an unresolved activation's
# snapshot is pinned on top (task #3696 exception inventory). The managed name
# grammar lives in `services.pitr.logical_dump_names`, shared with retention.
ACTIVATION_KEEP = 2
# Headroom against a stall, not an expected runtime: a full dump with
# checkpoint history takes about 6.3 min.
_DUMP_TIMEOUT_S = 60 * 60
# Heartbeat cadence while a dump or an encryption runs with a progress sink
# attached. An in-process snapshot (the PITR activation's logical floor) may run
# for many minutes without writing anything; a beat every 60 s keeps its
# operator's view alive instead of reading a healthy dump as a hung one.
_PROGRESS_INTERVAL_S = 60.0
# Bound the composition-sample connection (a dead DB must stall the backup log
# line only this long before degrading to "unavailable", never hang it).
_BREAKDOWN_CONNECT_TIMEOUT_S = 10
_TARGET_NAME_ATTEMPTS = 60
_backup_lock_guard = threading.RLock()
_backup_lock_state = threading.local()


def _cluster_tz() -> ZoneInfo:
    """The cluster wall clock every scheduling decision here is made in."""
    return ZoneInfo(settings.general.timezone)


def _require_aware(now: datetime) -> datetime:
    """A naive datetime is rejected rather than read as host-local: silently
    adopting the host's timezone is the exact failure this module was carrying."""
    if now.tzinfo is None:
        raise ValueError(f"backup needs a TZ-aware datetime, got naive {now!r}")
    return now


def _parse_stamp(stamp: str) -> datetime:
    """A managed dump's filename stamp as an aware UTC instant.

    The reading rules (UTC by construction; legacy stamps read in cluster
    time) live in `services.pitr.logical_dump_names.stamp_utc`.
    """
    return stamp_utc(stamp, _cluster_tz())


def backup_dir() -> Path:
    # `<home>/backups/db`: the home itself already scopes the cluster (path-only
    # identity), so the dump dir needs no per-cluster token. Pre-cutover dumps
    # under `backups/<cluster-name>` are left in place (at most ``backup_keep`` of
    # them); rotation continues in the new dir.
    return Path(settings.general.ava_home).expanduser() / "backups" / "db"


@contextmanager
def backup_lock(*, timeout_s: float | None = None) -> Generator[None]:
    """Serialize backup creation and verification across local processes.

    The lock is re-entrant within one thread, so a pre-update snapshot can hold
    it while calling `run_backup` and checking that dump's restore TOC.
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
            dumps.append((_parse_stamp(m["ts"]), path))
    return sorted(dumps)


def activation_snapshot(operation_id: str) -> Path | None:
    """The exact published dump owned by one durable activation operation."""
    suffix = f".{ACTIVATION_MARKER}-{operation_id}.dump.enc"
    matches = [
        path for _timestamp, path in _managed_dumps(backup_dir()) if path.name.endswith(suffix)
    ]
    if len(matches) > 1:
        raise RuntimeError("multiple snapshots belong to one PITR activation operation")
    return matches[0] if matches else None


def _is_pre_update(path: Path) -> bool:
    """Whether a managed dump is an update-kind snapshot rather than a daily dump."""
    m = DUMP_NAME_RE.match(path.name)
    return bool(m and m.group("kind") == PRE_UPDATE_MARKER)


def _is_activation(path: Path) -> bool:
    """Whether a dump is pinned by a not-yet-protected PITR operation."""
    m = DUMP_NAME_RE.match(path.name)
    return bool(m and (m.group("kind") or "").startswith(ACTIVATION_MARKER))


def _active_activation_pin(directory: Path) -> Path | None:
    if directory.resolve() != backup_dir().resolve():
        return None
    from services.pitr.activation_state import load_record
    from shared.paths import ava_home

    record = load_record(ava_home())
    if record is None or record.phase in {"protected", "rolled_back"}:
        return None
    if record.pre_activation_snapshot is None:
        return None
    pin = Path(record.pre_activation_snapshot)
    if pin.parent.resolve() != directory.resolve():
        raise RuntimeError("active PITR snapshot lies outside the managed backup directory")
    return pin


def active_activation_snapshot_name() -> str | None:
    """The file name of the in-flight activation operation's pinned snapshot.

    The retention planner mirrors the local prune's pin, so the off-site copy
    of the logical recovery floor survives while the activation is
    unresolved. None when no operation holds the pin.
    """
    pin = _active_activation_pin(backup_dir())
    return None if pin is None else pin.name


def is_due(now: datetime) -> bool:
    """True once the cluster clock has passed ``backup_hour`` with no dump for the
    current cluster day. `now` must be TZ-aware."""
    local_now = _require_aware(now).astimezone(_cluster_tz())
    if local_now.hour < settings.services.backup_hour:
        return False
    dumps = _managed_dumps(backup_dir())
    tz = _cluster_tz()
    return not dumps or dumps[-1][0].astimezone(tz).date() < local_now.date()


def _prune(directory: Path) -> list[Path]:
    """Delete managed dumps beyond retention: the newest ``backup_keep`` daily dumps
    plus the newest pre-update snapshot. Every migration-bearing `ava cluster
    update` writes one snapshot into this same pool, so without a separate slot
    the updates would silently shrink the daily window; the newest snapshot is
    always the most recent full dump before a migration, so it is kept."""
    dumps = _managed_dumps(directory)
    dailies = [
        (ts, path) for ts, path in dumps if not _is_pre_update(path) and not _is_activation(path)
    ]
    snapshots = [(ts, path) for ts, path in dumps if _is_pre_update(path)]
    activations = [(ts, path) for ts, path in dumps if _is_activation(path)]
    keep = set(dailies[-settings.services.backup_keep :]) | set(activations[-ACTIVATION_KEEP:])
    active_pin = _active_activation_pin(directory)
    if active_pin is not None:
        keep.update(item for item in activations if item[1] == active_pin)
    if snapshots:
        keep.add(snapshots[-1])
    removed: list[Path] = []
    for ts, path in dumps:
        if (ts, path) not in keep:
            path.unlink()
            removed.append(path)
    return removed


def dump_source() -> str:
    """The dial `pg_dump` reads this cluster's whole database through.

    A locally owned plane dumps as the administrator acting as the schema owner
    over the home's owner-only socket (`shared.pg_admin`): password-free,
    custody-checked against this home's postmaster, and independent of the
    write generations a rollout revokes, so a dump never needs, and never dies
    with, a delivered login. A remote-managed plane's provider URL
    (`direct_db_url`) is its only authority; `_passwordless_conninfo` keeps its
    password off argv. Both bypass PgBouncer: pg_dump holds one snapshot across
    many statements, which a transaction pooler cannot keep.
    """
    if settings.data_plane.is_remote:
        return direct_db_url()
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


def _key_file(directory: Path) -> Path:
    """Write the derived backup passphrase to a private temporary file."""
    fd, name = tempfile.mkstemp(prefix=".backup-key-", dir=directory)
    path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as key_file:
            key_file.write(hashlib.sha256(settings.data_plane.cluster_secret.encode()).hexdigest())
    except BaseException:
        with suppress(OSError):
            path.unlink(missing_ok=True)
        raise
    return path


def decrypt_artifact(artifact: Path, custom_dump: Path) -> None:
    """Decrypt one managed artifact into a custom-format dump.

    `custom_dump` is the raw `pg_dump --format=custom` archive for artifacts
    written by the current pipeline; for legacy `<db>-<ts>.dump.gz.enc`
    artifacts it is the gzip-compressed archive (call `gunzip_if_needed`).
    The caller owns `custom_dump` and removes it once consumed. Neither the
    cluster secret nor its derived passphrase is placed on argv.
    """
    custom_dump.touch(mode=0o600, exist_ok=False)
    custom_dump.chmod(0o600)
    key_file = _key_file(custom_dump.parent)
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
            raise RuntimeError(f"backup decrypt exited {proc.returncode}")
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


class _EncryptedFileSource:
    """A ``RestartableEncryptedSource`` over the published encrypted artifact.

    The store re-iterates the source (the Baidu backend hashes once and
    uploads once), so every iteration re-opens the seekable file: the bytes
    are deterministic for the artifact's lifetime — the publisher alone owns
    this path between the publish and the local prune.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._crc32c: str | None = None

    @property
    def ciphertext_size(self) -> int:
        return self._path.stat().st_size

    @property
    def ciphertext_crc32c(self) -> str:
        if self._crc32c is None:
            from services.pitr.checksums import CRC32C, digest_file

            self._crc32c = digest_file(CRC32C, str(self._path))
        return self._crc32c

    def iter_chunks(self) -> Iterable[bytes]:
        with self._path.open("rb") as source:
            while chunk := source.read(8 * 1024 * 1024):
                yield chunk


def _publish_offsite(artifact: Path) -> str | None:
    """Best-effort BlobStore-contract publish; never sacrifice the local artifact.

    Publishes the encrypted dump iff absent as ``{REMOTE_ROOT}/{name}`` on
    the configured backup store backend and logs the store-verified ACK. A
    missing or unconfigured store, or a failed publish, warns and retains the
    local artifact — the off-site leg stays optional, exactly as the Drive
    copy it replaces.
    """
    from services.pitr.store_factory import get_store_group

    try:
        store = get_store_group().restartable_streaming_object_store()
    except Exception:
        _log.exception("[backup] off-site store unavailable; local artifact retained")
        return None
    object_name = f"{REMOTE_ROOT}/{artifact.name}"
    try:
        ack = store.put_base_if_absent(
            source=_EncryptedFileSource(artifact),
            object_name=object_name,
            metadata={"ava-artifact-kind": "logical-backup"},
        )
    except Exception:
        _log.exception(
            "[backup] off-site publish of %s failed; local artifact retained", object_name
        )
        return None
    _log.info(
        "[backup] off-site published %s (size=%d, pin=%s, checksum=%s:%s)",
        object_name,
        ack.size,
        ack.pin_token,
        ack.checksum.algo,
        ack.checksum.value,
    )
    return object_name


def _available_target(
    directory: Path,
    dbname: str,
    now: datetime,
    *,
    pre_update: bool,
    pitr_activation: str | None,
) -> Path:
    """Return an unused managed dump path without replacing a prior snapshot.

    `pre_update` marks an `ava cluster update` snapshot with a kind segment so
    prune can give update-kind artifacts their own retention slot.
    """
    if pre_update and pitr_activation:
        raise ValueError("a backup cannot be both pre-update and PITR activation")
    if pitr_activation is not None and str(UUID(pitr_activation)) != pitr_activation:
        raise ValueError("PITR activation backup requires a canonical operation UUID")
    marker = f"{ACTIVATION_MARKER}-{pitr_activation}" if pitr_activation else PRE_UPDATE_MARKER
    kind = f".{marker}" if pre_update or pitr_activation else ""
    for offset_s in range(_TARGET_NAME_ATTEMPTS):
        stamp = (now + timedelta(seconds=offset_s)).astimezone(UTC).strftime(TS_FORMAT)
        target = directory / f"{dbname}-{stamp}{kind}.dump.enc"
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
    db_url: str | None = None,
    timeout_s: float = _DUMP_TIMEOUT_S,
    pre_update: bool = False,
    pitr_activation: str | None = None,
    publish: bool = True,
    progress: _ProgressSink | None = None,
    staging: Path | None = None,
) -> Path:
    """Dump the cluster DB into backup_dir() and prune; return the dump path.

    Plaintext and encrypted intermediates use `.partial` names; only the
    encrypted custom-format artifact is published after every pipeline step
    succeeds. A failed step removes its intermediates once its tool is reaped.
    `staging` writes into a scheduled operation's private controls instead
    and leaves publication and pruning to that operation's controller.

    `timeout_s` lets bounded callers such as the pre-update snapshot use a
    tighter ceiling than the daily backup default. `pre_update` names the
    artifact `<db>-<ts>.pre-update.dump.enc` so prune keeps it in its own
    retention slot (newest one) instead of consuming a daily-dump slot.
    `publish=False` keeps the completed artifact local, so off-site network
    latency cannot extend a pre-update snapshot.

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
            directory=directory,
            db_url=db_url,
            timeout_s=timeout_s,
            pre_update=pre_update,
            pitr_activation=pitr_activation,
            progress=progress,
        )
        if publish:
            _publish_offsite(target)
        if staging is None:
            _log_written(target, _prune(target.parent))
        return target


def prune_after_publish(target: Path) -> None:
    """Prune around a newly linked scheduled dump without waiting on the lock.

    Linking never replaces a managed name, so it needs no lock; pruning does.
    A busy lock (a weekly base capture can hold it for hours) defers pruning
    to the next backup instead of stalling the scheduler.
    """
    try:
        with backup_lock(timeout_s=0):
            removed = _prune(target.parent)
    except LockTimeoutError:
        _log.info("[backup] prune deferred while another backup owns the lock")
        removed = []
    _log_written(target, removed)


def _db_size_breakdown(db_url: str | None = None) -> str:
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
            psycopg.connect(db_url, autocommit=True, connect_timeout=_BREAKDOWN_CONNECT_TIMEOUT_S)
            if db_url is not None
            else connect(direct=True, autocommit=True)
        ) as conn:
            row = conn.execute(
                """
                SELECT pg_database_size(current_database()),
                       COALESCE(pg_total_relation_size(to_regclass('public.checkpoint_blobs')), 0),
                       COALESCE(pg_total_relation_size(to_regclass('public.checkpoints')), 0),
                       COALESCE(pg_total_relation_size(to_regclass('public.checkpoint_writes')), 0)
                """
            ).fetchone()
    except Exception:
        return "unavailable"
    assert row is not None  # noqa: S101 — aggregate over fixed tables always returns one row
    db, blobs, checkpoints, writes = (int(v) for v in row)
    checkpoint = blobs + checkpoints + writes
    rest = max(db - checkpoint, 0)
    return f"db={_mb(db)}MiB checkpoint={_mb(checkpoint)}MiB rest={_mb(rest)}MiB"


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
    db_url: str | None = None,
    timeout_s: float = _DUMP_TIMEOUT_S,
    pre_update: bool = False,
    pitr_activation: str | None = None,
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
    db_url = db_url if db_url is not None else dump_source()
    directory = ensure_private_dir(directory)
    db_conninfo, password = _passwordless_conninfo(db_url)
    dbname = cast(str, conninfo_to_dict(db_url)["dbname"])
    _log.info("[backup] db composition: %s", _db_size_breakdown(db_url))
    target = _available_target(
        directory,
        dbname,
        now,
        pre_update=pre_update,
        pitr_activation=pitr_activation,
    )
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
    parser = argparse.ArgumentParser(prog="python -m services.backup")
    parser.add_argument("--publish-offsite", type=Path, metavar="ARTIFACT")
    args = parser.parse_args(argv)
    if args.publish_offsite is None:
        parser.error("--publish-offsite is required")
    artifact = args.publish_offsite
    if not artifact.is_absolute():
        parser.error("--publish-offsite ARTIFACT must be an absolute path")
    _publish_offsite(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
