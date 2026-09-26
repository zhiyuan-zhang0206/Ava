"""Home lifecycle mutexes shared by local start/stop/pause and cluster recovery.

Two OS advisory locks live beside each other under ``$AVA_HOME``:

- ``resource_lock`` (``deploy-state.lifecycle.lock``) serializes long local
  start/stop/pause transitions with a bounded wait.
- ``lifecycle_lock`` (``deploy-state.owner.lock``) serializes the short
  pause-owner publication against recovery's proof and destructive action.

Each mutex has an atomically replaced ``.holder.json`` sidecar naming the last
holder's PID, purpose, start time and held/released state, so a bounded wait can
name who held it. The sidecar is diagnostic only; the OS lock is the authority.
The lock file names are stable: renaming them would split mutual exclusion
between processes built from different revisions.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import sys
from collections.abc import Generator
from pathlib import Path

import shared.paths
from shared.atomic_io import fsync_parent, write_text_atomic
from shared.platform import LockTimeoutError, file_lock

_LOCK_TIMEOUT_S = 5.0
_RESOURCE_LOCK_TIMEOUT_S = 30.0

_log = logging.getLogger("shared.ui_update_state")


def lifecycle_lock_path() -> Path:
    """Stable mutex for long local resource transitions and the hold probe."""
    return shared.paths.ava_home() / "deploy-state.lifecycle.lock"


def owner_lock_path() -> Path:
    """Short mutex for publishing ownership versus destructive recovery."""
    return shared.paths.ava_home() / "deploy-state.owner.lock"


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
        _log.warning("[ui-update-state] directory fsync failed after holder commit", exc_info=True)


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
                _log.warning("[ui-update-state] could not record lock holder", exc_info=True)
            try:
                yield
            finally:
                try:
                    _write_atomic(_holder_path(path), {**holder, "state": "released"})
                except OSError:
                    _log.warning(
                        "[ui-update-state] could not mark released lock holder", exc_info=True
                    )
    except LockTimeoutError as exc:
        if acquired:
            raise
        raise LockTimeoutError(
            f"{exc}; waiter purpose={purpose!r}; last holder: {_last_holder(path)}"
        ) from exc


@contextlib.contextmanager
def lifecycle_lock() -> Generator[None]:
    """Serialize short owner-publish and owner-recovery critical sections."""
    # The contextmanager generator enters through contextlib.__enter__; its
    # caller is the operation that actually owns this critical section.
    caller = sys._getframe(2)
    purpose = f"{caller.f_globals['__name__']}.{caller.f_code.co_name}"
    with _diagnostic_lock(owner_lock_path(), purpose=purpose, timeout_s=_LOCK_TIMEOUT_S):
        yield


@contextlib.contextmanager
def resource_lock(*, purpose: str, timeout_s: float = _RESOURCE_LOCK_TIMEOUT_S) -> Generator[None]:
    """Serialize long local start/stop/pause operations with bounded waiting."""
    with _diagnostic_lock(lifecycle_lock_path(), purpose=purpose, timeout_s=timeout_s):
        yield
