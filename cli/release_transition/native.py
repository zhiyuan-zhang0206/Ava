"""The one native executor adapter dispatch for release operations.

Linux submits a finite systemd unit; macOS bootstraps a finite launchd job that
runs the signed helper's finite mode. A recorded launch is always read back,
retired and continued by the adapter of its own recorded kind, independent of
the current host. A new launch uses the host's adapter or refuses: there is no
platform fallback and no direct-spawn path.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from pydantic import JsonValue

from cli.release_transition.request import PitrRequest, Request

LINUX = "linux-systemd-v1"
DARWIN = "darwin-launchd-v1"


def require_private_operation(path: Path, home: Path) -> None:
    """Launch inputs live only in canonical owner-controlled directories."""
    for directory in (home, home / "updates", path.parent):
        info = directory.lstat()
        if (
            directory.resolve(strict=True) != directory
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise ValueError("release launch requires canonical owner-controlled directories")
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise ValueError("release launch journal must be a private owner-only file")


def recorded_kind(record: Mapping[str, JsonValue]) -> JsonValue:
    """A launch record's adapter kind, classified exactly as the journal does.

    macOS records always carry their kind; a record without one keeps the
    original systemd contract (journal ``_darwin``), whose adapter validates it.
    """
    return record.get("kind", LINUX)


def for_launch(record: Mapping[str, JsonValue]) -> ModuleType:
    """The adapter that owns an already recorded launch; unknown kinds refuse."""
    kind = recorded_kind(record)
    if kind == LINUX:
        from cli.release_transition import launcher_linux

        return launcher_linux
    if kind == DARWIN:
        from cli.release_transition import launcher_macos

        return launcher_macos
    raise ValueError(f"unknown native executor kind: {kind!r}")


def _host_platform() -> str:
    return sys.platform


def for_host(request: Request | PitrRequest) -> ModuleType:
    """The adapter admitted for a new launch on this host, checked before effects."""
    from shared.os_boot_unit import systemd_running

    if systemd_running():
        from cli.release_transition import launcher_linux

        return launcher_linux
    if _host_platform() == "darwin":
        from cli.release_transition import launcher_macos

        launcher_macos.admit_request(request)
        return launcher_macos
    raise RuntimeError("this release executor requires a verified native adapter; no fallback")
