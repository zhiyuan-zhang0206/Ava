"""Warning-only ownership checks for files converge must be able to update."""

from __future__ import annotations

import os
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx
from shared.platform_backend import get_backend


def collect_ownership_warnings(ctx: ConvergeCtx) -> list[str]:
    """Return non-user-owned key $AVA_HOME paths, or nothing off POSIX."""
    if not get_backend().is_posix():
        return []
    current_uid = os.getuid()
    warnings: list[str] = []
    for path in _key_paths(ctx.ava_home):
        owner_uid = _owner_uid(path)
        if owner_uid == current_uid:
            continue
        warnings.append(
            f"{path}: owned by uid {owner_uid}, current uid {current_uid}; repair with: "
            f"{_repair_command(path)}"
        )
    return warnings


def ensure_ownership_preflight(ctx: ConvergeCtx) -> None:
    """Print and log ownership repairs while always allowing converge to proceed."""
    try:
        warnings = collect_ownership_warnings(ctx)
    except Exception as exc:
        print(f"  · ownership preflight skipped: {exc}", file=sys.stderr)
        return
    if not warnings:
        return

    print(
        "\n⚠  OWNERSHIP PREFLIGHT — $AVA_HOME paths belong to another user (start continues):",
        file=sys.stderr,
    )
    for warning in warnings:
        print(f"    {warning}", file=sys.stderr)
    print(
        "    Repair these paths before the next start; this warning never blocks converge.",
        file=sys.stderr,
    )
    _append_warnings(ctx.ava_home / "logs" / "ownership_preflight.log", warnings)


def _key_paths(ava_home: Path) -> tuple[Path, ...]:
    paths = (
        ava_home,
        ava_home / ".env",
        ava_home / "logs",
        ava_home / "configs",
        ava_home / "secrets",
        ava_home / "source",
    )
    return tuple(path for path in paths if path.exists())


def _owner_uid(path: Path) -> int:
    return path.stat().st_uid


def _repair_command(path: Path) -> str:
    import grp
    import pwd

    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
    except KeyError:
        user = str(os.getuid())
        group = str(os.getgid())
    return f"sudo chown -R {shlex.quote(user)}:{shlex.quote(group)} {shlex.quote(str(path))}"


def _append_warnings(path: Path, warnings: list[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as output:
            for warning in warnings:
                output.write(f"{stamp} {warning}\n")
    except OSError as exc:
        # A root-owned logs directory is the incident this preflight reveals;
        # failure to record there must not hide the console repair command.
        print(f"  · ownership preflight log unavailable: {exc}", file=sys.stderr)
