"""Install the host-global macOS permission-prompt LaunchAgent."""

from __future__ import annotations

import os
import platform
import plistlib
import subprocess
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx
from shared import proc

LABEL = "com.ava.permission-watcher"
_LAUNCHCTL_TIMEOUT_S = 30.0


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path() -> Path:
    return launch_agents_dir() / f"{LABEL}.plist"


def _replace_plist(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _restore_plist(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    _replace_plist(path, previous)


def render_permission_watcher_plist(ctx: ConvergeCtx) -> bytes:
    """Render the fixed-label prod watcher LaunchAgent."""
    repo = ctx.repo.resolve()
    log_path = ctx.ava_home.resolve() / "logs" / "permission-watcher.log"
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            str(repo / ".venv" / "bin" / "python"),
            str(repo / "services" / "permission_watcher" / "watcher.py"),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=False)


def ensure_permission_watcher(ctx: ConvergeCtx) -> None:
    """Write and load the watcher when its durable LaunchAgent spec changes.

    The converge registration is host-global and gateway-scoped: the watcher
    reads the prod unit's local PgBouncer URL, which a pure agent-runner unit
    does not carry. An unchanged plist is a strict no-op, including no launchd
    calls.
    """
    if platform.system() != "Darwin":
        return
    desired = render_permission_watcher_plist(ctx)
    path = _plist_path()
    if path.exists() and path.read_bytes() == desired:
        return
    previous = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    (ctx.ava_home / "logs").mkdir(parents=True, exist_ok=True)
    _replace_plist(path, desired)

    domain = f"gui/{os.getuid()}"
    try:
        proc.run_bounded(
            ["launchctl", "bootout", f"{domain}/{LABEL}"],
            timeout=_LAUNCHCTL_TIMEOUT_S,
            capture_output=True,
        )
        result = proc.run_bounded(
            ["launchctl", "bootstrap", domain, str(path)],
            timeout=_LAUNCHCTL_TIMEOUT_S,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        _restore_plist(path, previous)
        raise
    if result.returncode != 0:
        _restore_plist(path, previous)
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"launchctl bootstrap failed for {LABEL}: {detail or result.returncode}")
