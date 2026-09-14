"""Where throwaway Postgres clusters put their data — and how much room they need.

Owned by `shared/pg_tools.py`'s throwaway-cluster lifecycle; extracted here to
keep that module under its line ceiling (the same reason
`shared/pg_stall_watchdog.py` exists). Holds the host facts and the one policy:

- The **platform default base**: `/dev/shm` on Linux (RAM-backed, so scratch
  clusters get tmpfs speed — the historical choice), else the OS temp dir (macOS
  has no /dev/shm; its SSD-backed `$TMPDIR` is fast enough).
- The **disk fallback base**: `/var/tmp` where it exists (on the data volume,
  unlike the RAM-sized tmpfs), else the OS temp dir.
- The **selection policy** (`select_throwaway_base`): the operator override wins
  outright; a caller that declares its required capacity gets the disk fallback
  when the platform default cannot hold it, and a loud refusal when no base can.

Why the capacity step exists (2026-09-14 WSL incident): the throwaway cluster
defaulted to /dev/shm — 7.8 GiB on that host — while a full-restore drill of a
10.16 GiB artifact needs >=17 GiB of restored data. The postmaster died
mid-restore when the cluster outgrew the tmpfs; the drill surfaced only
`PQputCopyData: server closed the connection` and the host a dmesg signal 6. A
caller that knows its footprint (the restore drill) now asks for a base with room
before the cluster is created.

The override channel is the host-scoped `AVA_PG_THROWAWAY_BASE` settings field
(`settings.data_plane.pg_throwaway_base`), imported only when a base is selected:
this module is loaded by minimal contexts (test fixtures, smokes) that must not
pull the Settings tree any earlier.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from shared.log import logger

# Platform default base for throwaway data dirs: /dev/shm on Linux (RAM), else
# the OS temp dir (mac has no /dev/shm; its SSD-backed $TMPDIR is fast enough).
# A module attribute read at call time — tests pin it per test, and an operator
# can monkeypatch it in-process.
_tmpfs_base: str | None = None
if Path("/dev/shm").is_dir():  # noqa: S108 — deliberate tmpfs for throwaway cluster data
    _tmpfs_base = "/dev/shm"  # noqa: S108
# Windows: use %TEMP% (no /dev/shm equivalent)
elif sys.platform == "win32":
    _tmpfs_base = tempfile.gettempdir()


def default_base() -> Path:
    """The platform default base for throwaway data dirs — `/dev/shm` on Linux, the
    OS temp dir elsewhere. Read live so a per-test monkeypatched `_tmpfs_base` and
    the platform default both apply to each call."""
    return Path(_tmpfs_base or tempfile.gettempdir())


def disk_fallback_base() -> Path:
    """Disk-backed base used when the platform default cannot hold a caller's
    required capacity: `/var/tmp` where it exists (Linux/macOS — on the data volume,
    unlike the RAM-sized /dev/shm), else the OS temp dir."""
    var_tmp = Path("/var/tmp")  # noqa: S108 — deliberate disk fallback for scratch clusters
    if var_tmp.is_dir():
        return var_tmp
    return Path(tempfile.gettempdir())


def configured_base() -> Path | None:
    """The operator's explicit throwaway base — `settings.data_plane.pg_throwaway_base`
    (`AVA_PG_THROWAWAY_BASE`); None when unset. Imported lazily: this module is
    loaded by minimal contexts (test fixtures, smokes) and must not pull the
    Settings tree until a base is actually selected."""
    from shared.config import settings

    raw = settings.data_plane.pg_throwaway_base.strip()
    return Path(raw) if raw else None


def free_bytes(path: Path) -> int:
    """Free bytes on the filesystem backing `path` — the seam the base-selection
    tests replace, so they never depend on a real tmpfs."""
    return shutil.disk_usage(path).free


def format_bytes(n: int) -> str:
    """Bytes as a short human string (GiB, or MiB below 1 GiB) for capacity
    messages and logs."""
    if n >= 2**30:
        return f"{n / 2**30:.1f} GiB"
    return f"{n / 2**20:.0f} MiB"


class InsufficientThrowawaySpaceError(RuntimeError):
    """No candidate throwaway base offers the free space the caller requires."""


def throwaway_roots() -> tuple[Path, ...]:
    """Every directory a throwaway instance dir may exist under: the configured
    override (when set), the platform default `throwaway_postgres` hands to
    `mkdtemp`, and the disk fallback the restore path demotes to — deduped, order
    stable. The sweep and the port registry run over the whole set: an instance
    created under any of them must be found, reaped, and counted from there. On the
    platform default the instance dies with the tmpfs on reboot; on a disk base it
    does not, which is exactly why the owner-lock sweep — not the OS — is the
    durable reaper."""
    roots: list[Path] = []
    for root in (configured_base(), default_base(), disk_fallback_base()):
        if root is not None and root not in roots:
            roots.append(root)
    return tuple(roots)


def select_throwaway_base(required_bytes: int | None = None) -> Path:
    """Pick the base directory for a throwaway cluster's data dir.

    Order: the configured override (`AVA_PG_THROWAWAY_BASE`) when set — used as-is,
    with a warning when `required_bytes` clearly exceeds its free space; otherwise
    the platform default (`default_base`); otherwise, when `required_bytes` is
    given and the default cannot hold it, the disk fallback (`disk_fallback_base`).

    `required_bytes` is the caller's estimate of the cluster's peak footprint on its
    base — the restore drill passes a multiple of the artifact it restores. None
    (every other caller) skips the capacity step and keeps the platform default,
    the historical behavior.

    Raises:
        RuntimeError: the configured override is not an existing directory.
        InsufficientThrowawaySpaceError: `required_bytes` is given and no base
            offers it. Failing here names each base and its free space instead of
            dying mid-restore when the data outgrows its base (2026-09-14 WSL).
    """
    override = configured_base()
    if override is not None:
        if not override.is_dir():
            raise RuntimeError(
                f"AVA_PG_THROWAWAY_BASE={override} is not an existing directory — "
                "point it at a directory on a volume with room for scratch clusters"
            )
        if required_bytes is not None and free_bytes(override) < required_bytes:
            logger.warning(
                f"throwaway base override {override} offers "
                f"{format_bytes(free_bytes(override))}, below the requested "
                f"{format_bytes(required_bytes)} — proceeding anyway (explicit override)"
            )
        return override

    default = default_base()
    if required_bytes is None or free_bytes(default) >= required_bytes:
        return default

    fallback = disk_fallback_base()
    if fallback != default and free_bytes(fallback) >= required_bytes:
        logger.info(
            f"throwaway base demoted from {default} to {fallback}: "
            f"{format_bytes(required_bytes)} requested, "
            f"{format_bytes(free_bytes(default))} free on {default}"
        )
        return fallback

    offered = ", ".join(
        f"{path} ({format_bytes(free_bytes(path))} free)"
        for path in dict.fromkeys((default, fallback))
    )
    raise InsufficientThrowawaySpaceError(
        f"throwaway Postgres needs {format_bytes(required_bytes)} free — {offered}. "
        "Free space, or point AVA_PG_THROWAWAY_BASE at a larger volume."
    )
