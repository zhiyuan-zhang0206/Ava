"""Converge step: one-shot cleanup of the removed macOS permission-prompt watcher.

The TCC/ALF prompt observer (services/permission_watcher) was deleted
2026-08-26 by user ruling (task #1720: drop all TCC interception). Its
KeepAlive launchd job pointed at the deleted watcher.py, so after the next
rollout it would crash-loop; this step boots the job out and deletes its
plist. One-shot: once both are gone every later converge is a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cli.commands._converge_gate import _bootout_and_wait, _job_loaded
from cli.commands._converge_spec import ConvergeCtx
from shared.platform import IS_MACOS

LABEL = "com.ava.permission-watcher"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def remove_legacy_permission_watcher(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Boot out the removed permission-watcher LaunchAgent and drop its plist."""
    if not IS_MACOS:
        return
    if _job_loaded(LABEL):
        if not _bootout_and_wait(LABEL):
            print(
                f"  ⚠ legacy permission-watcher launchd job {LABEL} still loaded "
                "after bootout — boot it out manually before `ava start`",
                file=sys.stderr,
            )
            return  # keep the plist so the operator can see what is loaded
        print("  · legacy permission-watcher launchd job booted out")
    plist = _plist_path()
    if plist.exists():
        plist.unlink()
        print("  · legacy permission-watcher plist removed")
