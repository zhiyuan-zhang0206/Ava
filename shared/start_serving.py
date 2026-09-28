"""Generation-owned serving state for one ``ava start`` attempt.

Recovery cannot revive work until ordinary start proves the exact root and runtime
are serving. Native IPC binds this receipt to the root that answered readiness.  A new
attempt first publishes ``starting`` with a fresh generation; only the matching
attempt can publish ``serving`` after its readiness gate succeeds.  Therefore a
marker left by an earlier boot cannot admit recovery during the next boot.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self, cast

import psutil
from pydantic import model_validator

from shared.atomic_io import fsync_parent, write_text_atomic
from shared.native_process.ownership import OwnedProcess
from shared.paths import ava_home, run_dir
from shared.platform import file_lock
from shared.process_evidence import Digest, EvidenceModel, ExpectedProcess
from shared.runtime_interpreter import LoadedRuntimeIdentity, capture_loaded_runtime
from shared.verified_file import regular_bytes

_log = logging.getLogger("shared.start_serving")
_STATE_FILENAME = "start-serving.json"
_LOCK_FILENAME = "start-serving.lock"


def state_path() -> Path:
    """The per-unit serving state persisted across service processes."""
    return run_dir() / _STATE_FILENAME


def _lock_path() -> Path:
    return run_dir() / _LOCK_FILENAME


class RootBirth(EvidenceModel):
    """Local birth evidence only; never a fleet publication or resource fence."""

    home: str
    process: ExpectedProcess
    launch_digest: Digest
    runtime: LoadedRuntimeIdentity

    def same_generation(self, other: RootBirth) -> bool:
        own = OwnedProcess(self.process.pid, self.process.create_time, self.process.starttime)
        actual = OwnedProcess(other.process.pid, other.process.create_time, other.process.starttime)
        return (
            own.same_birth(actual)
            and self.home == other.home
            and self.launch_digest == other.launch_digest
            and self.runtime == other.runtime
        )


class ServingState(EvidenceModel):
    schema_version: Literal[2] = 2
    state: Literal["starting", "serving"]
    generation: str
    birth: RootBirth | None = None

    @model_validator(mode="after")
    def valid_phase(self) -> Self:
        uuid.UUID(self.generation)
        if (self.state == "serving") != (self.birth is not None):
            raise ValueError("serving requires exact root birth evidence")
        return self


def _read_state() -> ServingState | None:
    try:
        return ServingState.model_validate_json(regular_bytes(state_path()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _log.warning("[start-serving] cannot trust serving state: %s", exc)
        return None


def _observe_root() -> RootBirth:
    from shared.root_control.client import RootClient, RootClientError, native_identity

    home = ava_home().resolve(strict=True)
    response = RootClient(home / "run/ava-root/ava-root.sock", timeout=2).status()
    body = response.get("result")
    if response["ok"] is not True or not isinstance(body, dict):
        raise RootClientError("root did not provide its birth evidence")
    root = cast("dict[str, object]", body).get("root")
    if not isinstance(root, dict):
        raise RootClientError("root omitted its running identity")
    root = cast("dict[str, object]", root)
    if root.get("home") != str(home):
        raise RootClientError("root birth belongs to another home")
    if root.get("running") is not True:
        raise RootClientError("root is not running")
    process = native_identity(root)
    if not process.live():
        raise RootClientError("root birth ended before serving observation")
    return RootBirth.model_validate(
        {
            "home": str(home),
            "process": {
                "pid": process.pid,
                "create_time": process.birth,
                "starttime": process.starttime,
            },
            "launch_digest": root.get("launch_digest"),
            "runtime": root.get("runtime"),
        }
    )


def born_identity() -> ServingState | None:
    """Return only current exact root evidence; missing/unknown never permits."""
    state = _read_state()
    if state is None or state.state != "serving" or state.birth is None:
        return None
    try:
        if state.birth.same_generation(_observe_root()) and _read_state() == state:
            return state
        return None
    except (OSError, psutil.Error, ValueError, RuntimeError) as exc:
        _log.warning("[start-serving] cannot observe serving root: %s", exc)
        return None


def require_born_runtime() -> ServingState:
    """Bind this caller's code to a live start generation, with no DB grant."""
    state = born_identity()
    if state is None or state.birth is None:
        raise RuntimeError("loaded runtime has no matching live serving root")
    birth = state.birth
    if capture_loaded_runtime(Path(birth.home)) != birth.runtime or born_identity() != state:
        raise RuntimeError("loaded runtime has no matching live serving root")
    return state


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    fsync_parent(path)


def _sync_parent_or_log(path: Path) -> None:
    try:
        _fsync_parent(path)
    except OSError:
        # The replace or unlink is already visible. Preserve the safe state
        # transition rather than reporting a false failure to the caller.
        _log.warning("[start-serving] directory fsync failed after marker change", exc_info=True)


def _write_state(state: ServingState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path, state.model_dump_json(), encoding="utf-8", mode=0o600, prefix=".start-serving-"
    )
    _sync_parent_or_log(path)


def begin_start() -> str:
    """Invalidate any previous success and return this start's generation."""
    generation = str(uuid.uuid4())
    with file_lock(_lock_path()):
        _write_state(ServingState(state="starting", generation=generation))
    return generation


def mark_serving(generation: str, *, runtime: LoadedRuntimeIdentity) -> bool:
    """Publish serving only when ``generation`` still owns the start attempt."""
    with file_lock(_lock_path()):
        current = _read_state()
        if current is None or current.state != "starting" or current.generation != generation:
            return False
        birth = _observe_root()
        if birth.runtime != runtime:
            raise RuntimeError("serving root differs from the admitted loaded runtime")
        _write_state(ServingState(state="serving", generation=generation, birth=birth))
        return True


def is_serving() -> bool:
    """Whether a completed current-generation start permits recovery actions."""
    return born_identity() is not None


@contextmanager
def recovery_permitted() -> Generator[bool]:
    """Authorize one recovery action while excluding a new start or stop.

    The caller keeps this context through the actual revive or launch. That
    makes the serving check and its effect one indivisible operation relative
    to the start generation change, rather than a racy check-then-act pair.
    """
    with file_lock(_lock_path()):
        yield born_identity() is not None


def clear_serving() -> None:
    """Remove this unit's serving authority before a deliberate stop."""
    with file_lock(_lock_path()):
        path = state_path()
        path.unlink(missing_ok=True)
        _sync_parent_or_log(path)
