"""Trace mirror hygiene — the disk guards for the sidecar's JSONL mirror.

The local OTel Collector sidecar writes one JSONL span mirror under
`$AVA_HOME/traces/` (`spans.jsonl` active + rotated backups). The agent-side
passes in this module keep that directory bounded: compression of rotated
segments (the collector's file exporter cannot compress its own rotations),
day-based retention, a hard directory cap, and the disk-watermark guard.
Every helper here is idempotent and OSError-tolerant — the traces dir is
shared by every agent process on the host, so a peer can unlink a file this
process just globbed mid-pass.

Split out of `shared/trace.py` (2026-08-30) when the boot-path arming
machinery pushed that file past its line budget; `shared/trace.py` re-exports
these names for the existing test surface.
"""

from __future__ import annotations

import gzip
import re
import shutil
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from shared.log import logger
from shared.paths import traces_dir

__all__ = [
    "_disk_usage",
    "_disk_watermark_exceeded",
    "_enforce_dir_cap",
    "_gzip_old_mirror",
    "_mirror_day",
    "_mirror_epoch",
    "_mirror_size",
    "_mirror_sort_key",
    "_prune_old_mirror",
]

# Mirror filenames (sidecar file exporter layout since task #1266):
#   spans.jsonl                          — the ACTIVE file the collector appends to
#   spans-<ISO-timestamp>-size.jsonl     — collector-rotated backups (timberjack
#                                          1.4.5, pinned by otelcol-contrib 0.155.0,
#                                          appends the trigger reason to the
#                                          backup name: `-size` / `-time`;
#                                          `spans-2026-08-27T03-29-10.942-size.jsonl`,
#                                          stamped in UTC)
#   spans-<ISO-timestamp>.jsonl          — older collector-rotated backups
#                                          (pre-0.155.0 lumberjack, no reason suffix)
#   spans-YYYYMMDD-<pid>.jsonl           — legacy agent-side mirror (pre-#1266)
#   spans.cut-YYYYMMDD.jsonl             — manual "cut" of the active file (ops
#                                          one-off): the collector keeps appending
#                                          to the renamed inode until its next size
#                                          rotation, then a fresh spans.jsonl is born
#   <any of the above>.gz                — compressed by `_gzip_old_mirror` once the
#                                          file stops being written; consumers
#                                          (`ava trace ship`, inspect-a-trace) read
#                                          these transparently
# Old and rotated names parse their day from the name; the active file has no day
# stamp and is therefore never a retention/prune target (the collector's own
# rotation bounds it).
_MIRROR_DAY_RE = re.compile(r"^spans-(\d{8})-(\d+)\.jsonl(?:\.gz)?$")
_MIRROR_ROTATED_RE = re.compile(
    r"^spans-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})\.\d+"
    r"(?:-(?:size|time))?\.jsonl(?:\.gz)?$"
)
_MIRROR_CUT_RE = re.compile(r"^spans\.cut-(\d{8})\.jsonl(?:\.gz)?$")


def _mirror_day(path: Path) -> date | None:
    """Day stamp of a mirror file from its name; None when it carries none.

    Old agent-side files stamp `spans-YYYYMMDD-<pid>.jsonl`; the collector's
    rotated backups stamp `spans-YYYY-MM-DDTHH-MM-SS.<ms>(-size|-time)?.jsonl`
    (timberjack 1.4.5 appends the trigger reason; optional `.gz` after the
    agent-side gzip pass); manual cuts stamp `spans.cut-YYYYMMDD.jsonl`; the
    ACTIVE `spans.jsonl` carries no stamp at all (never pruned, bounded by
    the collector's own rotation).
    """
    m = _MIRROR_DAY_RE.match(path.name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007 — date-only
    m = _MIRROR_ROTATED_RE.match(path.name)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _MIRROR_CUT_RE.match(path.name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007 — date-only
    return None


def _mirror_epoch(path: Path) -> int:
    """Sub-day order key: pid for legacy files, epoch seconds for rotated
    backups (both share the day key in `_mirror_sort_key`); 0 for the active
    file. A name that parses as neither sorts last with day +inf — an
    unrecognized file is never the deletion target."""
    m = _MIRROR_DAY_RE.match(path.name)
    if m:
        return int(m.group(2))
    m = _MIRROR_ROTATED_RE.match(path.name)
    if m:
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp())
    # A manual cut carries no sub-day order key; the day stamp alone orders it.
    return 0


def _prune_old_mirror(retention_days: int) -> None:
    """Delete mirror files whose day stamp is older than retention_days.

    Recording is always-on but shipping may be off, so the mirror grows
    unbounded without this. Pruned on each agent start (the only frequent
    recording entry); current-day files (the only ones being appended) are never
    in range. A window not shipped within retention_days is gone — that is the
    documented retention contract, not silent loss.
    """
    if retention_days <= 0:
        return
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).date()
    removed = 0
    # `.jsonl.gz` included: the gzip pass renames old segments, and retention
    # must still reach them (their day stamp parses from the name).
    for path in traces_dir().glob("spans*.jsonl*"):
        day = _mirror_day(path)
        if day is not None and day < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info(
            "pruned old trace mirror files",
            event="trace",
            removed=removed,
            cutoff=cutoff.isoformat(),
        )


def _gzip_old_mirror(grace_seconds: int = 60) -> int:
    """gzip rotated (non-active) mirror files; return the number compressed.

    The collector's file exporter cannot compress its own rotated backups
    (fileexporter 0.155.0 forces timberjack Compression "none"; its zstd
    option applies to the ACTIVE stream and would break the grep surface),
    so this pass — running on each agent start next to the retention prune —
    compresses every mirror file except the ACTIVE `spans.jsonl`. JSONL
    compresses ~5-10x, so the recovery mirror's disk footprint drops
    accordingly. Consumers read `.jsonl.gz` transparently: `ava trace ship`
    continues from the per-file watermark keyed by the base name (a gzip pass
    never strands unshipped lines), and the inspect-a-trace mirror fetcher
    decompresses on read.

    Idempotent: already-compressed `.gz` files are skipped; a crash between
    writing the `.gz` and unlinking the original leaves both, and the next
    pass re-compresses (overwrites) and unlinks. A manual "cut" of the active
    file (the collector keeps appending to the renamed inode until its next
    size rotation) is skipped within `grace_seconds` of its last write, so a
    file still being appended is never compressed mid-write; timberjack-
    rotated backups are static by construction and only the cut edge case is
    guarded. The tail written after the rename-away is bounded loss under the
    mirror's documented contract (it still reached Tempo through the
    collector's live fan-out). A negative grace compresses regardless of
    mtime (tests).
    """
    cutoff = time.time() - grace_seconds
    compressed = 0
    for path in traces_dir().glob("spans*.jsonl*"):
        if path.name == "spans.jsonl" or path.name.endswith(".gz"):
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        gz_path = Path(f"{path}.gz")
        try:
            # mtime=0 keeps the compressed bytes deterministic (tests, dedup).
            with (
                path.open("rb") as src,
                gzip.GzipFile(gz_path, mode="wb", compresslevel=6, mtime=0) as dst,
            ):
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            path.unlink(missing_ok=True)
        except OSError:
            gz_path.unlink(missing_ok=True)
            continue
        compressed += 1
    if compressed:
        logger.info(
            "gzipped old trace mirror files",
            event="trace",
            compressed=compressed,
            grace_seconds=grace_seconds,
        )
    return compressed


def _mirror_sort_key(p: Path) -> tuple[int, int]:
    """Oldest-first key for a mirror file (day, sub-day order).

    Day comes from the name (`_mirror_day`); sub-day is the numeric pid for
    legacy files (string order would prune `...-1000` before `...-999`,
    deleting a newer file) or the epoch seconds of a rotated backup. The
    ACTIVE `spans.jsonl` (no day stamp) sorts last — the cap prune never
    deletes the file being written unless the cap is absurdly small (same
    contract as retention: bounded disk, documented loss, never silent). No
    stat — the file may vanish mid-parse.
    """
    day = _mirror_day(p)
    if day is not None:
        return (day.toordinal(), _mirror_epoch(p))
    return (2**31, 0)


def _mirror_size(p: Path) -> int:
    """Size of a mirror file, 0 if it vanished between glob and stat.

    The traces dir is shared by every agent process on the host; a peer's
    prune can unlink a file this process already globbed, and a bare stat
    would then raise FileNotFoundError — out of the agent boot path, which
    calls `_enforce_dir_cap` with no try/except (a peer pruning at the same
    moment could kill an agent start).
    """
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _enforce_dir_cap(max_mb: int) -> int:
    """Delete oldest mirror files until the traces directory fits under `max_mb`.

    Iterates oldest-first by day stamp + numeric pid (`_mirror_sort_key`).
    Never deletes the file currently being written (the newest, by key) unless
    the cap is absurdly small — same contract as retention: bounded disk,
    documented loss, never silent. Returns the number of files deleted. No-op
    when max_mb <= 0.
    """
    if max_mb <= 0:
        return 0
    cap_bytes = max_mb * 1024 * 1024
    files = sorted(traces_dir().glob("spans*.jsonl*"), key=_mirror_sort_key)
    total = sum(_mirror_size(p) for p in files)
    removed = 0
    for p in files:
        if total <= cap_bytes:
            break
        total -= _mirror_size(p)
        p.unlink(missing_ok=True)
        removed += 1
    if removed:
        logger.info(
            "trace mirror over size cap — removed oldest files",
            event="trace",
            removed=removed,
            cap_mb=max_mb,
        )
    return removed


def _disk_usage() -> tuple[float, int] | None:
    """(usage fraction, free bytes) of the traces dir's data disk.

    The mirror is written to $AVA_HOME/traces, so the data disk is the one that
    matters. A stat failure (exotic filesystem) returns None — the guard must
    never be the reason recording is off.
    """
    try:
        usage = shutil.disk_usage(traces_dir())
    except OSError:
        return None
    return usage.used / usage.total, usage.free


def _disk_watermark_exceeded(watermark: float) -> bool:
    """True when the data disk's usage fraction exceeds the watermark.

    `watermark >= 1.0` disables the guard (the fraction can never exceed 1.0).
    """
    if watermark >= 1.0:
        return False
    usage = _disk_usage()
    return usage is not None and usage[0] > watermark
