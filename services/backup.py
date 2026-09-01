"""Daily local Postgres backup, driven by the gateway scheduler daemon.

One `pg_dump --format=custom` per day into `$AVA_HOME/backups/db/`,
keeping the newest BACKUP_KEEP dumps. `services.backup_scheduler.daemon`
calls `is_due()` and `run_backup()` independently of watchdog rounds. The
scheduler runs at the first wake after BACKUP_HOUR cluster time with no dump
for the current cluster day, so a host that was down at 03:00 catches up.
The scheduler is the only production caller of `is_due()` and `run_backup()`.

Two clocks, deliberately different ones:

- **When to run** is the cluster wall clock (`AVA_TIMEZONE`, cluster-pinned):
  "03:00" means the same instant for every gateway in the fleet, and it does
  not move when a machine does. Reading the host's OS timezone instead used to
  skip backups outright — carry a laptop from Asia/Shanghai to US/Pacific and
  the newest dump's local date sits ahead of the new local date, so `is_due`
  returned False (silently, with no error to notice) until the calendar caught
  up.
- **What a dump is called** is UTC, stamped `Z`: `<db>-YYYYMMDDTHHMMSSZ.dump.enc`.
  Prune deletes the oldest dumps by this stamp, so the ordering must be a
  total order over real instants. A local-time name is not: in the DST fall-back
  hour the same wall clock names two instants an hour apart, and re-parsing it
  as naive-then-local made "which backup do we delete" depend on which offset
  the parse happened to pick.

Local dumps guard against bad migrations / accidental deletes / DB
corruption. Storage interface: `run_backup` is
`dump -> encrypt -> optional off-site publish -> prune`. The off-site leg
publishes the encrypted artifact through the shared backup store contract
(`services.pitr.store_factory`'s `RestartableStreamingObjectStore`
`put_base_if_absent` — the same backend switch as the physical PITR plane),
so the GCS / Baidu Netdisk adapters are shared and the Drive-sync-folder copy
is gone. The publish is best-effort and immutable: it happens iff absent,
the ACK (pin_token, size, checksum) is the store-verified identity, and a
missing/unavailable/failed store warns and keeps the local artifact — it never
discards it. Remote objects are append-only: the store contract deliberately
has no delete verb (neither does the physical plane), so remote retention is a
shared planner concern (dry-run today) — see `future/infra/pg-backup.md`.
The dump is `pg_dump --format=custom` with zstd compression — custom format
already compresses the archive, so the pre-2026-08-27 pipeline's extra `gzip`
stage (a second compression pass over already-compressed bytes) is gone. The
benchmark on the production DB (2026-08-27) measured the removed gzip pass at
13-47 s for a <1% size gain. Legacy artifacts named `<db>-<ts>.dump.gz.enc`
stay managed and restorable (see `gunzip_if_needed`).

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
import re
import subprocess
import tempfile
import threading
from collections.abc import Generator, Iterable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from shared.config import settings
from shared.db import connect, direct_db_url
from shared.pg_tools import pg_tool
from shared.platform import file_lock
from shared.private_storage import ensure_private_dir, ensure_private_file

_log = logging.getLogger(__name__)

BACKUP_HOUR = 3  # cluster time; the first tick at/after this hour runs the day's backup
BACKUP_KEEP = 7  # a week of daily dumps: a bad migration found a day later must not have overwritten the last good copy
ACTIVATION_KEEP = 2
_PRE_UPDATE_MARKER = "pre-update"  # filename kind segment for `ava cluster update` snapshots
_ACTIVATION_MARKER = "pitr-activation"
# Off-site logical-dump namespace: a sibling of the PITR prefix (the
# retention inventory scopes its listing to the PITR prefix, so these names
# never surface as unknown inventory entries).
_REMOTE_ROOT = "ava-logical"
# Generous ceiling for one dump (the DB is far smaller); see the comment at the
# subprocess.run call for why an unbounded pg_dump is not acceptable here.
# 60 min: a full dump with checkpoint history takes about 6.3 min. This is
# headroom against a stall, not an expected runtime.
_DUMP_TIMEOUT_S = 60 * 60
# Bound the composition-sample connection (a dead DB must stall the backup log
# line only this long before degrading to "unavailable", never hang it).
_BREAKDOWN_CONNECT_TIMEOUT_S = 10
_NAME_RE = re.compile(
    r"^(?P<db>.+)-(?P<ts>\d{8}T\d{6}Z|\d{8}-\d{6})"
    r"(?:\.(?P<kind>pre-update|pitr-activation(?:-[0-9a-f-]{36})?))?\.dump(?:\.gz\.enc|\.enc)?$"
)
_TS_FORMAT = "%Y%m%dT%H%M%SZ"  # UTC, offset-bearing by construction
_LEGACY_TS_FORMAT = "%Y%m%d-%H%M%S"  # pre-cutover names: wall clock, no offset
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
    """A managed dump's filename stamp as an aware UTC instant."""
    if stamp.endswith("Z"):
        return datetime.strptime(stamp, _TS_FORMAT).replace(tzinfo=UTC)
    # Legacy name: no offset was ever recorded, so read it in cluster time.
    # `.replace(tzinfo=...)` (fold=0) keeps the reading deterministic through
    # the DST fall-back hour, where `.astimezone()` on a naive value used to
    # pick an offset out of the host's clock.
    legacy = datetime.strptime(stamp, _LEGACY_TS_FORMAT).replace(tzinfo=_cluster_tz())
    return legacy.astimezone(UTC)


def backup_dir() -> Path:
    # `<home>/backups/db`: the home itself already scopes the cluster (path-only
    # identity), so the dump dir needs no per-cluster token. Pre-cutover dumps
    # under `backups/<cluster-name>` are left in place (at most BACKUP_KEEP of
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
        m = _NAME_RE.match(path.name)
        if m and path.is_file():
            dumps.append((_parse_stamp(m["ts"]), path))
    return sorted(dumps)


def activation_snapshot(operation_id: str) -> Path | None:
    """The exact published dump owned by one durable activation operation."""
    suffix = f".{_ACTIVATION_MARKER}-{operation_id}.dump.enc"
    matches = [
        path for _timestamp, path in _managed_dumps(backup_dir()) if path.name.endswith(suffix)
    ]
    if len(matches) > 1:
        raise RuntimeError("multiple snapshots belong to one PITR activation operation")
    return matches[0] if matches else None


def _is_pre_update(path: Path) -> bool:
    """Whether a managed dump is an update-kind snapshot rather than a daily dump."""
    m = _NAME_RE.match(path.name)
    return bool(m and m.group("kind") == _PRE_UPDATE_MARKER)


def _is_activation(path: Path) -> bool:
    """Whether a dump is pinned by a not-yet-protected PITR operation."""
    m = _NAME_RE.match(path.name)
    return bool(m and (m.group("kind") or "").startswith(_ACTIVATION_MARKER))


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


def is_due(now: datetime) -> bool:
    """True once the cluster clock has passed BACKUP_HOUR with no dump for the
    current cluster day. `now` must be TZ-aware."""
    local_now = _require_aware(now).astimezone(_cluster_tz())
    if local_now.hour < BACKUP_HOUR:
        return False
    dumps = _managed_dumps(backup_dir())
    tz = _cluster_tz()
    return not dumps or dumps[-1][0].astimezone(tz).date() < local_now.date()


def _prune(directory: Path) -> list[Path]:
    """Delete managed dumps beyond retention: the newest BACKUP_KEEP daily dumps
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
    keep = set(dailies[-BACKUP_KEEP:]) | set(activations[-ACTIVATION_KEEP:])
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

    Publishes the encrypted dump iff absent as ``{_REMOTE_ROOT}/{name}`` on
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
    object_name = f"{_REMOTE_ROOT}/{artifact.name}"
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
    marker = f"{_ACTIVATION_MARKER}-{pitr_activation}" if pitr_activation else _PRE_UPDATE_MARKER
    kind = f".{marker}" if pre_update or pitr_activation else ""
    for offset_s in range(_TARGET_NAME_ATTEMPTS):
        stamp = (now + timedelta(seconds=offset_s)).astimezone(UTC).strftime(_TS_FORMAT)
        target = directory / f"{dbname}-{stamp}{kind}.dump.enc"
        if not target.exists():
            return target
    raise RuntimeError("could not choose a distinct backup filename within 60 seconds")


def run_backup(
    now: datetime | None = None,
    *,
    db_url: str | None = None,
    timeout_s: float = _DUMP_TIMEOUT_S,
    pre_update: bool = False,
    pitr_activation: str | None = None,
    publish: bool = True,
) -> Path:
    """Dump the cluster DB into backup_dir() and prune; return the dump path.

    Plaintext and encrypted intermediates use `.partial` names; only the
    encrypted custom-format artifact is published after every pipeline step
    succeeds.

    `timeout_s` lets bounded callers such as the pre-update snapshot use a
    tighter ceiling than the daily backup default. `pre_update` names the
    artifact `<db>-<ts>.pre-update.dump.enc` so prune keeps it in its own
    retention slot (newest one) instead of consuming a daily-dump slot.
    `publish=False` keeps the completed artifact local; the rollout prepare
    phase uses it so off-site network latency cannot extend maintenance.
    """
    with backup_lock():
        return _run_backup(
            now,
            db_url=db_url,
            timeout_s=timeout_s,
            pre_update=pre_update,
            pitr_activation=pitr_activation,
            publish=publish,
        )


def _db_size_breakdown(db_url: str | None = None) -> str:
    """One-line DB composition for the backup log: total, the LangGraph
    checkpoint tables, and everything else.

    The checkpoint tables dominate DB size and dump time; logging them on
    every backup makes each artifact carry its own baseline for growth
    tracking. The frozen PG `events` archive is no longer part of the
    composition — it was dropped with the task #1281/#1823 cleanup (its data
    lives in the Loki archive stream and the cold pg_dump archive). Best-effort
    by contract: a failure (e.g. a fresh cluster missing the tables) degrades
    to "unavailable" and never fails the backup.

    `db_url` mirrors `_run_backup`'s override: the composition is measured on
    the SAME database the dump will read. `_run_backup` always passes the
    resolved admin-plane direct URL; a None `db_url` (direct callers) falls
    back to the same settings-derived connection.
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


def _run_backup(
    now: datetime | None = None,
    *,
    db_url: str | None = None,
    timeout_s: float = _DUMP_TIMEOUT_S,
    pre_update: bool = False,
    pitr_activation: str | None = None,
    publish: bool = True,
) -> Path:
    """Write one managed dump while `backup_lock` is held.

    Pipeline: `pg_dump --format=custom --compress=zstd:3` (the custom archive
    compresses in-dump; there is no separate gzip stage), then AES-CBC
    encryption. `timeout_s` bounds every subprocess; the caller owns the lock.
    """
    now = _require_aware(now) if now is not None else datetime.now(UTC)
    # direct_db_url() (the admin-plane direct URL, derived from the
    # registry record): pg_dump needs a real Postgres session (it holds a consistent
    # snapshot across many statements); running it through a transaction pooler is
    # meaningless and breaks. Admin plane bypasses PgBouncer.
    db_url = db_url if db_url is not None else direct_db_url()
    directory = ensure_private_dir(backup_dir())
    # A run interrupted mid-dump (e.g. the process tree killed during a rollout)
    # leaves `.partial` files behind. The name never matches `_NAME_RE`, so the
    # due/prune logic ignores them and they pile up. Sweep them before writing a
    # new dump; a fresh partial from THIS run is created after the sweep.
    for stale in directory.glob("*.partial"):
        with suppress(OSError):
            stale.unlink()
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
    try:
        # The scheduler owns this subprocess in its own process, so its bound
        # cannot delay watchdog supervision. subprocess.run kills the child on
        # expiry and TimeoutExpired lets the scheduler schedule its retry.
        proc = subprocess.run(  # noqa: S603
            dump_cmd,
            capture_output=True,
            check=False,
            env=dump_env,
            timeout=timeout_s,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump exited {proc.returncode}")

        encrypted_partial.touch(mode=0o600, exist_ok=False)
        encrypted_partial.chmod(0o600)
        key_file = _key_file(directory)
        try:
            proc = subprocess.run(  # noqa: S603
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
                capture_output=True,
                check=False,
                timeout=timeout_s,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"backup encryption exited {proc.returncode}")
        finally:
            with suppress(OSError):
                key_file.unlink(missing_ok=True)
        encrypted_partial.rename(target)
        ensure_private_file(target)
    finally:
        # Cleanup failure must not mask a pipeline error.
        for partial in (dump_partial, encrypted_partial):
            with suppress(OSError):
                partial.unlink(missing_ok=True)
    if publish:
        _publish_offsite(target)
    removed = _prune(directory)
    _log.info(
        "[backup] wrote %s (%.1f MiB), pruned %d",
        target,
        target.stat().st_size / 2**20,
        len(removed),
    )
    return target


def _main(argv: list[str] | None = None) -> int:
    """Run the detached, best-effort off-site backup publisher."""
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
