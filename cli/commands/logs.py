"""Delete expired local logs from the narrow operator-approved allowlist."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil

from shared.config import settings
from shared.paths import logs_dir

_MANAGED_LOG_NAME = re.compile(
    r"(?:"
    r"ava-agent-[0-9]+\.out\.log"
    r"|ava-agent-[0-9]+-shell-[0-9]+-[a-z][a-z0-9-]*\.(?:out|host)\.log"
    r"|[a-z][a-z0-9-]*\.[0-9]{4}-[0-9]{2}-[0-9]{2}_"
    r"[0-9]{2}-[0-9]{2}-[0-9]{2}_[0-9]+\.log"
    r")"
)


@dataclass(frozen=True)
class RetentionCandidate:
    path: Path
    mtime: float
    size_bytes: int


@dataclass(frozen=True)
class RetentionFailure:
    path: Path
    error: OSError


def _active_log_paths(logs_path: Path) -> set[Path]:
    """Resolved top-level log paths currently held open by visible processes."""
    root = logs_path.resolve()
    active: set[Path] = set()
    for process in psutil.process_iter():
        try:
            opened_files = process.open_files()
        except (psutil.Error, OSError):
            continue
        for opened in opened_files:
            path = Path(opened.path).resolve()
            if path.parent == root:
                active.add(path)
    return active


def _retention_candidates(
    logs_path: Path, cutoff: float, active_paths: set[Path]
) -> tuple[list[RetentionCandidate], list[RetentionFailure]]:
    candidates: list[RetentionCandidate] = []
    failures: list[RetentionFailure] = []
    with os.scandir(logs_path) as entries:
        for entry in entries:
            if _MANAGED_LOG_NAME.fullmatch(entry.name) is None:
                continue
            path = Path(entry.path)
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if path.resolve() in active_paths:
                    continue
                file_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                failures.append(RetentionFailure(path=path, error=exc))
                continue
            if file_stat.st_mtime >= cutoff:
                continue
            candidates.append(
                RetentionCandidate(
                    path=path,
                    mtime=file_stat.st_mtime,
                    size_bytes=file_stat.st_size,
                )
            )
    return sorted(candidates, key=lambda candidate: candidate.path.name), failures


def _utc_mtime(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def cmd_logs_retention(
    *,
    older_than_days: int | None,
    dry_run: bool,
    logs_path: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Apply local log retention and return a CLI exit code."""
    target = logs_dir() if logs_path is None else logs_path
    current = datetime.now(UTC) if now is None else now
    retention_days = (
        settings.observability.log_retention_days if older_than_days is None else older_than_days
    )
    cutoff = (current - timedelta(days=retention_days)).timestamp()
    try:
        candidates, scan_failures = _retention_candidates(target, cutoff, _active_log_paths(target))
    except OSError as exc:
        candidates = []
        scan_failures = [RetentionFailure(path=target, error=exc)]
    total_bytes = sum(candidate.size_bytes for candidate in candidates)
    for failure in scan_failures:
        print(
            f"retention_error\tpath={failure.path}\terror={failure.error}",
            file=sys.stderr,
        )

    if dry_run:
        for candidate in candidates:
            print(
                "retention_candidate"
                f"\tmtime={_utc_mtime(candidate.mtime)}"
                f"\tsize_bytes={candidate.size_bytes}"
                f"\tpath={candidate.path}"
            )
        print(
            "retention_summary"
            f"\tmode=dry-run\tfiles={len(candidates)}\tbytes={total_bytes}"
            f"\tfailed={len(scan_failures)}"
        )
        return 1 if scan_failures else 0

    deleted = 0
    reclaimed_bytes = 0
    failed = len(scan_failures)
    for candidate in candidates:
        try:
            candidate.path.unlink()
        except OSError as exc:
            failed += 1
            print(
                f"retention_error\tpath={candidate.path}\terror={exc}",
                file=sys.stderr,
            )
            continue
        deleted += 1
        reclaimed_bytes += candidate.size_bytes
    print(
        "retention_summary"
        f"\tmode=delete\tdeleted={deleted}\tbytes={reclaimed_bytes}\tfailed={failed}"
    )
    return 1 if failed else 0
