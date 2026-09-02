"""Generation-owned serving state for one ``ava start`` attempt.

Recovery controllers start inside the sessions that ``ava start`` launches, but
must not revive work until that command has proved the host is serving.  A new
attempt first publishes ``starting`` with a fresh generation; only the matching
attempt can publish ``serving`` after its readiness gate succeeds.  Therefore a
marker left by an earlier boot cannot admit recovery during the next boot.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

from shared.paths import run_dir
from shared.platform import file_lock

_log = logging.getLogger("shared.start_serving")
_SCHEMA_VERSION = 1
_STATE_FILENAME = "start-serving.json"
_LOCK_FILENAME = "start-serving.lock"


def state_path() -> Path:
    """The per-unit serving state persisted across service processes."""
    return run_dir() / _STATE_FILENAME


def _lock_path() -> Path:
    return run_dir() / _LOCK_FILENAME


def _read_state() -> tuple[Literal["starting", "serving"], str] | None:
    try:
        raw = json.loads(state_path().read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("[start-serving] cannot read serving state: %s", exc)
        return None
    if not isinstance(raw, dict):
        _log.warning("[start-serving] invalid serving state root")
        return None
    parsed = cast("dict[str, object]", raw)
    if parsed.get("schema_version") != _SCHEMA_VERSION:
        _log.warning("[start-serving] unknown serving state schema")
        return None
    state = parsed.get("state")
    generation = parsed.get("generation")
    if state not in ("starting", "serving") or not isinstance(generation, str) or not generation:
        _log.warning("[start-serving] invalid serving state fields")
        return None
    return state, generation


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_parent_or_log(path: Path) -> None:
    try:
        _fsync_parent(path)
    except OSError:
        # The replace or unlink is already visible. Preserve the safe state
        # transition rather than reporting a false failure to the caller.
        _log.warning("[start-serving] directory fsync failed after marker change", exc_info=True)


def _write_state(state: Literal["starting", "serving"], generation: str) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=".start-serving-", suffix=".tmp")
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {"schema_version": _SCHEMA_VERSION, "state": state, "generation": generation},
                stream,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)  # noqa: PTH105 — atomic marker commit
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _sync_parent_or_log(path)


def begin_start() -> str:
    """Invalidate any previous success and return this start's generation."""
    generation = str(uuid.uuid4())
    with file_lock(_lock_path()):
        _write_state("starting", generation)
    return generation


def mark_serving(generation: str) -> bool:
    """Publish serving only when ``generation`` still owns the start attempt."""
    with file_lock(_lock_path()):
        current = _read_state()
        if current != ("starting", generation):
            return False
        _write_state("serving", generation)
        return True


def is_serving() -> bool:
    """Whether a completed current-generation start permits recovery actions."""
    state = _read_state()
    return state is not None and state[0] == "serving"


@contextmanager
def recovery_permitted() -> Generator[bool]:
    """Authorize one recovery action while excluding a new start or stop.

    The caller keeps this context through the actual revive or launch. That
    makes the serving check and its effect one indivisible operation relative
    to the start generation change, rather than a racy check-then-act pair.
    """
    with file_lock(_lock_path()):
        state = _read_state()
        yield state is not None and state[0] == "serving"


def clear_serving() -> None:
    """Remove this unit's serving authority before a deliberate stop."""
    with file_lock(_lock_path()):
        path = state_path()
        path.unlink(missing_ok=True)
        _sync_parent_or_log(path)
