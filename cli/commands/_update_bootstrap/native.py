"""Exact launchd quiesce and recovery for the restricted immutable ops hop.

Only secret-free, positively unloaded bootstrap definitions are admitted. A
loaded label does not prove launchd's effective argv matches the on-disk plist.
The first source-to-image cutover must retire legacy loaded jobs separately;
this primitive never turns an unknown effective job into shutdown authority.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import stat
import sys
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from shared.managed_writer_observation import ExpectedUnitWriters
from shared.native_job_observation import (
    launchd_loaded_state,
    read_launchd_definition,
)
from shared.runtime_release import ReleaseRejectedError
from shared.updater_recovery import BootstrapRecoveryJournal, LaunchdRecovery
from shared.verified_file import regular_bytes


def _path(label: str) -> Path:
    directory = Path.home() / "Library" / "LaunchAgents"
    if directory.resolve(strict=True) != directory or directory.stat().st_uid != os.getuid():
        raise ReleaseRejectedError("bootstrap launchd directory is not canonical and owned")
    return directory / f"{label}.plist"


def _loaded(label: str, until: datetime) -> bool:
    result = launchd_loaded_state(label, until)
    if result is None:
        raise ReleaseRejectedError("bootstrap launchd loaded state is unknown")
    return result


def _check_definition(
    item: LaunchdRecovery, expected: ExpectedUnitWriters, argv: list[str]
) -> None:
    value: object = plistlib.loads(item.definition.encode())
    if not isinstance(value, dict):
        raise ReleaseRejectedError("bootstrap launchd definition must be a dictionary")
    raw = cast("dict[str, object]", value)
    allowed = {
        "Label",
        "ProgramArguments",
        "EnvironmentVariables",
        "RunAtLoad",
        "KeepAlive",
        "WorkingDirectory",
        "StandardOutPath",
        "StandardErrorPath",
    }
    if set(raw) - allowed:
        raise ReleaseRejectedError("bootstrap launchd definition has unsupported launch conditions")
    if (
        raw.get("Label") != item.label
        or raw.get("ProgramArguments") != argv
        or raw.get("EnvironmentVariables") != {"AVA_HOME": expected.home}
        or raw.get("KeepAlive", False) is not False
        or not isinstance(raw.get("RunAtLoad", False), bool)
        or raw.get("WorkingDirectory", expected.home) != expected.home
    ):
        raise ReleaseRejectedError("launchd definition is not a passive restricted bootstrap")
    if Path(item.custody).parent != Path(expected.home) / "run":
        raise ReleaseRejectedError("launchd custody escapes its unit run directory")
    _custody_path(item)
    _check_log_paths(raw, expected.home)


def _check_log_paths(raw: dict[str, object], home: str) -> None:
    for key in ("StandardOutPath", "StandardErrorPath"):
        if key in raw:
            value = raw[key]
            if not isinstance(value, str) or not Path(value).is_relative_to(home):
                raise ReleaseRejectedError("bootstrap launchd log path escapes its unit")
            if str(Path(value)) != value or ".." in Path(value).parts:
                raise ReleaseRejectedError("bootstrap launchd log path is not normalized")


def _capture_one(label: str, until: datetime, home: str) -> LaunchdRecovery:
    body = read_launchd_definition(label)
    if body is None:
        raise ReleaseRejectedError("prepared launchd definition disappeared")
    info = _path(label).lstat()
    if info.st_uid != os.getuid():
        raise ReleaseRejectedError("bootstrap launchd definition is not owned")
    if _loaded(label, until):
        raise ReleaseRejectedError("loaded launchd definition has no verified effective binding")
    item = LaunchdRecovery(
        label=label,
        definition=body.decode("utf-8"),
        loaded=False,
        custody=str(Path(home) / "run" / f"bootstrap-launcher-{uuid.uuid4().hex}.held"),
        mode=cast("Literal[384, 420]", stat.S_IMODE(info.st_mode)),
    )
    if read_launchd_definition(item.label) != body or _loaded(item.label, until) != item.loaded:
        raise ReleaseRejectedError("bootstrap launchd facts changed during capture")
    return item


def capture_launchd(
    expected: ExpectedUnitWriters,
    argv: list[str],
    until: datetime,
    *,
    retained: tuple[LaunchdRecovery, ...] | None = None,
) -> tuple[LaunchdRecovery, ...]:
    """Bind exact prepared definitions before journaling any native mutation."""
    if not expected.launchers or any(item.kind != "launchd" for item in expected.launchers):
        raise ReleaseRejectedError("restricted Mac hop requires exact native launchd ownership")
    snapshots: list[LaunchdRecovery] = []
    for launcher in expected.launchers:
        if retained is None:
            item = _capture_one(launcher.name, until, expected.home)
        else:
            matches = [item for item in retained if item.label == launcher.name]
            if len(matches) != 1:
                raise ReleaseRejectedError("retained launchd originals differ from the inventory")
            item = matches[0]
        if hashlib.sha256(item.definition.encode()).hexdigest() != launcher.definition_digest:
            raise ReleaseRejectedError(
                "bootstrap launchd definition differs from its prepared digest"
            )
        _check_definition(item, expected, argv)
        snapshots.append(item)
    if retained is not None and len(retained) != len(snapshots):
        raise ReleaseRejectedError("retained launchd originals contain an extra launcher")
    return tuple(sorted(snapshots, key=lambda item: item.label))


def prepare_launchd(
    expected: ExpectedUnitWriters,
    argv: list[str],
    until: datetime,
    journal: BootstrapRecoveryJournal | None,
) -> tuple[LaunchdRecovery, ...]:
    if sys.platform != "darwin":
        return ()
    return capture_launchd(expected, argv, until, retained=journal.launchd if journal else None)


def _definition_state(item: LaunchdRecovery) -> bytes | None:
    body = read_launchd_definition(item.label)
    if body is not None:
        info = _path(item.label).lstat()
        if (
            body != item.definition.encode()
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != item.mode
        ):
            raise ReleaseRejectedError(
                "launchd definition changed; refusing to overwrite another writer"
            )
    return body


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _custody_path(item: LaunchdRecovery) -> Path:
    """Private destination held by the updater's existing exclusive ownership."""
    path = Path(item.custody)
    parent = path.parent
    info = parent.stat()
    if (
        not path.is_absolute()
        or parent.resolve(strict=True) != parent
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
        or not path.name.startswith("bootstrap-launcher-")
        or path.suffix != ".held"
        or info.st_dev != _path(item.label).parent.stat().st_dev
    ):
        raise ReleaseRejectedError(
            "launchd custody requires an owner-controlled same-filesystem run directory"
        )
    return path


def _custody_state(item: LaunchdRecovery) -> bytes | None:
    path = _custody_path(item)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    body = regular_bytes(path)
    if (
        body != item.definition.encode()
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != item.mode
    ):
        raise ReleaseRejectedError(
            "moved launchd definition changed; custody retained for recovery"
        )
    return body


def _take_custody(item: LaunchdRecovery) -> None:
    source, custody = _path(item.label), _custody_path(item)
    if custody.exists():
        raise ReleaseRejectedError("launchd custody is already occupied")
    # The destination is a fresh private journaled name under updater ownership.
    # Atomic rename preserves whichever inode actually occupied the public path.
    os.rename(source, custody)  # noqa: PTH104 -- explicit atomic custody transfer.
    _sync_directory(source.parent)
    _sync_directory(custody.parent)
    try:
        _custody_state(item)
    except ReleaseRejectedError:
        # Put the concurrent writer's exact inode back only if its name remains
        # free. Otherwise retain both their new public file and the moved inode.
        try:
            os.link(custody, source)
            _sync_directory(source.parent)
        except FileExistsError:
            raise ReleaseRejectedError(
                "concurrent launchd definitions retained in both locations"
            ) from None
        raise


def quiesce_launchd(
    snapshots: tuple[LaunchdRecovery, ...], until: datetime, authorize: Callable[[], None]
) -> None:
    """Move positively unloaded originals into durable private custody."""
    for item in snapshots:
        body, held = _definition_state(item), _custody_state(item)
        if _loaded(item.label, until):
            raise ReleaseRejectedError("launchd definition gained an unaccounted loaded job")
        if body is not None:
            authorize()
            _take_custody(item)
            authorize()
        elif held is None:
            raise ReleaseRejectedError("launchd definition disappeared without recorded custody")
    verify_quiesced(snapshots, until)


def verify_quiesced(snapshots: tuple[LaunchdRecovery, ...], until: datetime) -> None:
    for item in snapshots:
        if (
            _custody_state(item) is None
            or read_launchd_definition(item.label) is not None
            or _loaded(item.label, until)
        ):
            raise ReleaseRejectedError("bootstrap launchd relauncher is not positively removed")


def restore_launchd(
    snapshots: tuple[LaunchdRecovery, ...], until: datetime, authorize: Callable[[], None]
) -> None:
    """Restore the retained inode without overwriting a public concurrent write."""
    for item in snapshots:
        body, held = _definition_state(item), _custody_state(item)
        if _loaded(item.label, until):
            raise ReleaseRejectedError("launchd restore found an unaccounted loaded job")
        if body is None:
            if held is None:
                raise ReleaseRejectedError("launchd original and custody are both absent")
            authorize()
            os.link(_custody_path(item), _path(item.label))
            _sync_directory(_path(item.label).parent)
            authorize()
        if _definition_state(item) is None or _loaded(item.label, until):
            raise ReleaseRejectedError("launchd restore differs from the retained original")
        if held is not None:
            authorize()
            # Only this updater owns the private custody name; public files are
            # never unlinked by their pathname after a check.
            _custody_path(item).unlink()
            _sync_directory(_custody_path(item).parent)
            authorize()
