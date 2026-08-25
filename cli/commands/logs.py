"""Delete expired local logs from the narrow operator-approved allowlist."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil

from shared.config import settings
from shared.paths import logs_dir

_MANAGED_LOG_NAME = re.compile(
    r"(?:"
    r"(?P<agent>ava-agent-[0-9]+\.out\.log)"
    r"|(?P<shell>ava-agent-[0-9]+-shell-[0-9]+-[a-z][a-z0-9-]*\.(?:out|host)\.log)"
    r"|(?P<service>[a-z][a-z0-9_-]*)\.[0-9]{4}-[0-9]{2}-[0-9]{2}_"
    r"[0-9]{2}-[0-9]{2}-[0-9]{2}_[0-9]+\.log"
    r")"
)

_FAMILY_DEFAULT_DAYS = {
    "agent": 15,
    "shell": 7,
    "gateway": 30,
    "ops": 30,
    "watchdog": 30,
    "other": 3,
}


@dataclass(frozen=True)
class RetentionCandidate:
    path: Path
    family: str
    retention_days: int
    mtime: float
    size_bytes: int


@dataclass(frozen=True)
class RetentionFailure:
    path: Path
    error: OSError


def _log_family(match: re.Match[str]) -> str:
    """Return the C retention family for one allowlisted filename."""
    if match["agent"] is not None:
        return "agent"
    if match["shell"] is not None:
        return "shell"

    service = match["service"]
    if service.startswith("gateway"):
        return "gateway"
    if service == "ops" or service.startswith(("ops-", "ops_")):
        return "ops"
    if service.endswith(("-watchdog", "_watchdog")):
        return "watchdog"
    if service.startswith("agent-"):
        return "agent"
    return "other"


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
    logs_path: Path,
    current: datetime,
    family_days: Mapping[str, int],
    active_paths: set[Path],
) -> tuple[list[RetentionCandidate], list[RetentionFailure]]:
    candidates: list[RetentionCandidate] = []
    failures: list[RetentionFailure] = []
    with os.scandir(logs_path) as entries:
        for entry in entries:
            match = _MANAGED_LOG_NAME.fullmatch(entry.name)
            if match is None:
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
            family = _log_family(match)
            retention_days = family_days[family]
            cutoff = (current - timedelta(days=retention_days)).timestamp()
            if file_stat.st_mtime >= cutoff:
                continue
            candidates.append(
                RetentionCandidate(
                    path=path,
                    family=family,
                    retention_days=retention_days,
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
    family_days: Mapping[str, int] | None = None,
    dry_run: bool,
    logs_path: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Apply local log retention and return a CLI exit code."""
    if older_than_days is not None and family_days is not None:
        raise ValueError("--older-than and --family-days are mutually exclusive")

    target = logs_dir() if logs_path is None else logs_path
    current = datetime.now(UTC) if now is None else now
    if family_days is None:
        retention_days = (
            settings.observability.log_retention_days
            if older_than_days is None
            else older_than_days
        )
        resolved_family_days = dict.fromkeys(_FAMILY_DEFAULT_DAYS, retention_days)
    else:
        overrides = dict(family_days)
        if "default" in overrides:
            if "other" in overrides:
                raise ValueError("--family-days cannot specify both default and other")
            overrides["other"] = overrides.pop("default")
        resolved_family_days = _FAMILY_DEFAULT_DAYS | overrides

    try:
        candidates, scan_failures = _retention_candidates(
            target,
            current,
            resolved_family_days,
            _active_log_paths(target),
        )
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
            family_fields = ""
            if family_days is not None:
                family_fields = f"\tfamily={candidate.family}\tdays={candidate.retention_days}"
            print(
                "retention_candidate"
                f"{family_fields}"
                f"\tmtime={_utc_mtime(candidate.mtime)}"
                f"\tsize_bytes={candidate.size_bytes}"
                f"\tpath={candidate.path}"
            )
        if family_days is not None:
            for family in sorted(resolved_family_days):
                family_candidates = [
                    candidate for candidate in candidates if candidate.family == family
                ]
                print(
                    "retention_family"
                    f"\tfamily={family}"
                    f"\tdays={resolved_family_days[family]}"
                    f"\tfiles={len(family_candidates)}"
                    f"\tbytes={sum(candidate.size_bytes for candidate in family_candidates)}"
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
