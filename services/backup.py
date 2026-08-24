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
- **What a dump is called** is UTC, stamped `Z`: `<db>-YYYYMMDDTHHMMSSZ.dump.gz.enc`.
  Prune deletes the oldest dumps by this stamp, so the ordering must be a
  total order over real instants. A local-time name is not: in the DST fall-back
  hour the same wall clock names two instants an hour apart, and re-parsing it
  as naive-then-local made "which backup do we delete" depend on which offset
  the parse happened to pick.

Local dumps guard against bad migrations / accidental deletes / DB
corruption. Storage interface: `run_backup` is
`dump -> gzip -> encrypt -> optional Drive copy -> prune`; a remote object
store can replace the Drive-copy step — tracked in `future/infra/pg-backup.md`.

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

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from shared.config import settings
from shared.db import direct_db_url
from shared.google_drive import find_writable_google_drive
from shared.pg_tools import pg_tool
from shared.private_storage import ensure_private_dir, ensure_private_file

_log = logging.getLogger(__name__)

BACKUP_HOUR = 3  # cluster time; the first tick at/after this hour runs the day's backup
BACKUP_KEEP = 7  # a week of daily dumps: a bad migration found a day later must not have overwritten the last good copy
# Generous ceiling for one dump (the DB is far smaller); see the comment at the
# subprocess.run call for why an unbounded pg_dump is not acceptable here.
# 60 min: a full dump with checkpoint history takes about 6.3 min. This is
# headroom against a stall, not an expected runtime.
_DUMP_TIMEOUT_S = 60 * 60
_NAME_RE = re.compile(r"^(?P<db>.+)-(?P<ts>\d{8}T\d{6}Z|\d{8}-\d{6})\.dump(?:\.gz\.enc)?$")
_TS_FORMAT = "%Y%m%dT%H%M%SZ"  # UTC, offset-bearing by construction
_LEGACY_TS_FORMAT = "%Y%m%d-%H%M%S"  # pre-cutover names: wall clock, no offset
_OFFSITE_ROOT = "Ava Backups"


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
    """Delete managed dumps beyond the newest BACKUP_KEEP; return what was removed."""
    removed: list[Path] = []
    for _ts, path in _managed_dumps(directory)[:-BACKUP_KEEP]:
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


def decrypt_artifact(artifact: Path, compressed_dump: Path) -> None:
    """Decrypt one managed artifact into its gzip-compressed custom dump.

    The caller owns `compressed_dump` and removes it once it has been unzipped.
    Neither the cluster secret nor its derived passphrase is placed on argv.
    """
    compressed_dump.touch(mode=0o600, exist_ok=False)
    compressed_dump.chmod(0o600)
    key_file = _key_file(compressed_dump.parent)
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
                str(compressed_dump),
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


def _offsite_directory() -> Path | None:
    """The optional private Google Drive directory for this cluster's artifacts."""
    drive = find_writable_google_drive()
    if drive is None:
        return None
    # A home path is the cluster identity. Hash it for a Drive-safe, stable
    # directory name without exposing the local checkout layout to collaborators.
    scope = hashlib.sha256(str(Path(settings.general.ava_home).expanduser()).encode()).hexdigest()[
        :16
    ]
    directory = drive / _OFFSITE_ROOT / scope
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _copy_offsite(artifact: Path) -> None:
    """Best-effort encrypted Drive copy; never sacrifice the local artifact."""
    try:
        directory = _offsite_directory()
    except OSError:
        _log.exception("[backup] encrypted off-site directory unavailable; local artifact retained")
        return
    if directory is None:
        _log.warning("[backup] no writable Google Drive folder; local artifact retained")
        return
    target = directory / artifact.name
    partial = target.with_name(target.name + ".partial")
    try:
        shutil.copyfile(artifact, partial)
        if partial.stat().st_size != artifact.stat().st_size:
            with suppress(OSError):
                partial.unlink(missing_ok=True)
            _log.error("[backup] off-site copy byte count mismatch; local artifact retained")
            return
        partial.replace(target)
        _prune(directory)
    except OSError:
        with suppress(OSError):
            partial.unlink(missing_ok=True)
        _log.exception("[backup] encrypted off-site copy failed; local artifact retained")


def run_backup(now: datetime | None = None, *, db_url: str | None = None) -> Path:
    """Dump the cluster DB into backup_dir() and prune; return the dump path.

    Plaintext and encrypted intermediates use `.partial` names; only the
    encrypted gzip artifact is published after every pipeline step succeeds.
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
    dbname = conninfo_to_dict(db_url)["dbname"]
    stem = f"{dbname}-{now.astimezone(UTC).strftime(_TS_FORMAT)}"
    target = directory / f"{stem}.dump.gz.enc"
    dump_partial = directory / f"{stem}.dump.partial"
    gzip_partial = directory / f"{stem}.dump.gz.partial"
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
            timeout=_DUMP_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump exited {proc.returncode}")

        gzip_partial.touch(mode=0o600, exist_ok=False)
        gzip_partial.chmod(0o600)
        with gzip_partial.open("wb") as compressed:
            proc = subprocess.run(  # noqa: S603
                ["gzip", "--stdout", str(dump_partial)],
                stdout=compressed,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_DUMP_TIMEOUT_S,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"gzip exited {proc.returncode}")

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
                    str(gzip_partial),
                    "-out",
                    str(encrypted_partial),
                ],
                capture_output=True,
                check=False,
                timeout=_DUMP_TIMEOUT_S,
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
        for partial in (dump_partial, gzip_partial, encrypted_partial):
            with suppress(OSError):
                partial.unlink(missing_ok=True)
    _copy_offsite(target)
    removed = _prune(directory)
    _log.info(
        "[backup] wrote %s (%.1f MiB), pruned %d",
        target,
        target.stat().st_size / 2**20,
        len(removed),
    )
    return target
