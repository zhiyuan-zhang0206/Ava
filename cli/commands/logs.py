"""Rotate and delete local logs within narrow operator-approved boundaries."""

from __future__ import annotations

import os
import re
import shutil
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
    r"|(?P<svcout>ava-[a-z][a-z0-9_-]*\.out\.log)"
    r"|(?P<rotlog>loki\.log\.[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|prometheus\.log\.[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|dbg-stdout\.log\.[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"|(?P<rotout>ava-[a-z][a-z0-9_-]*\.out\.log\.[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r")"
)

_ROTATED_LOG_ARCHIVE = re.compile(r".+\.log\.[0-9]{4}-[0-9]{2}-[0-9]{2}")

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


def _service_family(service: str) -> str:
    if service.startswith("gateway"):
        return "gateway"
    if service == "ops" or service.startswith(("ops-", "ops_")):
        return "ops"
    if service.endswith(("-watchdog", "_watchdog")):
        return "watchdog"
    if service.startswith("agent-"):
        return "agent"
    return "other"


def _log_family(match: re.Match[str]) -> str:
    """Return the C retention family for one allowlisted filename."""
    if match["agent"] is not None:
        return "agent"
    if match["shell"] is not None:
        return "shell"
    if match["rotlog"] is not None:
        return "other"

    service = match["service"]
    if service is None:
        service_stdout = match["svcout"] or match["rotout"]
        service = service_stdout.removeprefix("ava-").split(".out.log", 1)[0]
    return _service_family(service)


def _maintenance_roots(logs_path: Path) -> tuple[Path, ...]:
    """Top-level roots maintained for an `$AVA_HOME/logs` path."""
    if logs_path.name != "logs":
        return (logs_path,)
    return (logs_path, logs_path.parent / "lgtm" / "native" / "logs")


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


def _retention_scan(
    target: Path,
    current: datetime,
    family_days: Mapping[str, int],
) -> tuple[list[RetentionCandidate], list[RetentionFailure]]:
    candidates: list[RetentionCandidate] = []
    failures: list[RetentionFailure] = []
    for root in _maintenance_roots(target):
        try:
            root_candidates, root_failures = _retention_candidates(
                root,
                current,
                family_days,
                _active_log_paths(root),
            )
        except FileNotFoundError as exc:
            if root != target:
                continue
            failures.append(RetentionFailure(path=root, error=exc))
            continue
        except OSError as exc:
            failures.append(RetentionFailure(path=root, error=exc))
            continue
        candidates.extend(root_candidates)
        failures.extend(root_failures)
    return sorted(candidates, key=lambda candidate: str(candidate.path)), failures


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

    candidates, scan_failures = _retention_scan(
        target,
        current,
        resolved_family_days,
    )
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


def _rotation_name_allowed(name: str, *, native: bool) -> bool:
    if not native:
        return name.endswith(".out.log")
    return (
        name.endswith(".log")
        and not name.startswith("grafana")
        and _ROTATED_LOG_ARCHIVE.fullmatch(name) is None
    )


def _print_rotation_state(path: Path, copied_bytes: int, action: str) -> None:
    print(f"rotate_state\tpath={path}\tbytes={copied_bytes}\taction={action}")


def _rotation_entries(root: Path, target: Path) -> tuple[list[os.DirEntry[str]], bool]:
    try:
        with os.scandir(root) as scanned:
            return sorted(scanned, key=lambda entry: entry.name), False
    except FileNotFoundError as exc:
        if root != target:
            return [], False
        print(f"rotate_error\tpath={root}\terror={exc}", file=sys.stderr)
    except OSError as exc:
        print(f"rotate_error\tpath={root}\terror={exc}", file=sys.stderr)
    return [], True


def _rotate_entry(
    entry: os.DirEntry[str],
    *,
    native: bool,
    current: datetime,
    archive_suffix: str,
    size_threshold: int,
    dry_run: bool,
) -> bool:
    if not _rotation_name_allowed(entry.name, native=native):
        return False
    path = Path(entry.path)
    try:
        if not entry.is_file(follow_symlinks=False):
            return False
        file_stat = entry.stat(follow_symlinks=False)
        archive = Path(f"{path}.{archive_suffix}")
        triggered = file_stat.st_size >= size_threshold or (
            datetime.fromtimestamp(file_stat.st_mtime, UTC).date() != current.date()
        )
        if not triggered or os.path.lexists(archive):
            _print_rotation_state(path, 0, "kept")
            return False
        if not dry_run:
            shutil.copyfile(path, archive)
            with path.open("r+b") as stream:
                stream.truncate(0)
        _print_rotation_state(path, file_stat.st_size, "rotated")
    except OSError as exc:
        print(f"rotate_error\tpath={path}\terror={exc}", file=sys.stderr)
        return True
    return False


def cmd_logs_rotate(
    *,
    dry_run: bool,
    size_mib: int = 64,
    logs_path: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Copytruncate oversized or prior-UTC-day stdout logs."""
    if size_mib <= 0:
        raise ValueError("--size-mib must be a positive integer")

    target = logs_dir() if logs_path is None else logs_path
    current = datetime.now(UTC) if now is None else now
    archive_suffix = current.strftime("%Y-%m-%d")
    size_threshold = size_mib * 1024 * 1024
    failed = False

    for root in _maintenance_roots(target):
        entries, scan_failed = _rotation_entries(root, target)
        failed |= scan_failed
        native = root != target
        for entry in entries:
            failed |= _rotate_entry(
                entry,
                native=native,
                current=current,
                archive_suffix=archive_suffix,
                size_threshold=size_threshold,
                dry_run=dry_run,
            )

    return 1 if failed else 0
