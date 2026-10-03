"""Home lifecycle mutex shared by local start/stop.

``resource_lock`` (``deploy-state.lifecycle.lock``) is an OS advisory lock under
``$AVA_HOME`` that serializes long local start/stop transitions with a
bounded wait.

The lock has an atomically replaced ``.holder.json`` sidecar naming the last
holder's PID, purpose, start time and held/released state, so a bounded wait can
name who held it. The sidecar is diagnostic only; the OS lock is the authority.
The lock file name is stable: renaming it would split mutual exclusion between
processes built from different revisions.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
from collections.abc import Generator
from pathlib import Path

import base.paths
from base.host.atomic_io import fsync_parent, write_text_atomic
from base.native_process.os_platform import LockTimeoutError, file_lock

_RESOURCE_LOCK_TIMEOUT_S = 30.0

_log = logging.getLogger("base.deploy.lifecycle.home_lifecycle_locks")


def lifecycle_lock_path() -> Path:
    """Stable mutex for long local resource transitions and the hold probe."""
    return base.paths.ava_home() / "deploy-state.lifecycle.lock"


def _holder_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.holder.json")


def _last_holder(path: Path) -> str:
    try:
        data = json.loads(_holder_path(path).read_text())
        return (
            f"pid={data['pid']} purpose={data['purpose']!r} "
            f"since={data['since']} state={data['state']}"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    fsync_parent(path)


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path,
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        prefix=".deploy-state-",
    )
    try:
        _fsync_parent(path)
    except OSError:
        # os.replace is the visible commit point; durability is degraded, but
        # the committed sidecar stays truthful.
        _log.warning(
            "[home-lifecycle-locks] directory fsync failed after holder commit", exc_info=True
        )


@contextlib.contextmanager
def _diagnostic_lock(path: Path, *, purpose: str, timeout_s: float) -> Generator[None]:
    """Hold an OS mutex with an atomic, best-effort postmortem holder record.

    The OS lock is authority; the sidecar is diagnostic only. Keep the last
    released holder so a timeout racing release still names the recent actor.
    """
    acquired = False
    try:
        with file_lock(path, timeout_s=timeout_s):
            acquired = True
            holder = {
                "pid": os.getpid(),
                "purpose": purpose,
                "since": dt.datetime.now(dt.UTC).isoformat(),
            }
            try:
                _write_atomic(_holder_path(path), {**holder, "state": "held"})
            except OSError:
                _log.warning("[home-lifecycle-locks] could not record lock holder", exc_info=True)
            try:
                yield
            finally:
                try:
                    _write_atomic(_holder_path(path), {**holder, "state": "released"})
                except OSError:
                    _log.warning(
                        "[home-lifecycle-locks] could not mark released lock holder", exc_info=True
                    )
    except LockTimeoutError as exc:
        if acquired:
            raise
        raise LockTimeoutError(
            f"{exc}; waiter purpose={purpose!r}; last holder: {_last_holder(path)}"
        ) from exc


@contextlib.contextmanager
def resource_lock(*, purpose: str, timeout_s: float = _RESOURCE_LOCK_TIMEOUT_S) -> Generator[None]:
    """Serialize long local start/stop operations with bounded waiting."""
    with _diagnostic_lock(lifecycle_lock_path(), purpose=purpose, timeout_s=timeout_s):
        yield
