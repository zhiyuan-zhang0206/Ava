"""The generation handoff: what crosses an in-place root upgrade (exec).

An upgrade replaces the root process image with ``os.execve`` — same pid, same
parent, same inherited children — while the tree keeps running (B4: "exec
replacement, same pid, same attribution"). This module owns both halves of
that boundary:

- the handoff file, ``<run_dir>/handoff.json``: written atomically by the
  outgoing generation just before its exec, consumed by the incoming one;
- the adoption of inherited children: re-attaching ``waitpid`` to still-running
  units so the successor manages the SAME processes — it never respawns them.

Protocol:

1. ``Supervisor.upgrade()`` (under its mutation lock) snapshots every unit —
   its desired state, and for live generations pid/pgid/started_at — into
   ``handoff.json``.
2. The daemon writes the accepted response, flushes it, then execs itself:
   same pid, same argv, environment carrying the takeover marker.
3. The successor's startup validates the file against its own pid (exec keeps
   the pid, so ``writer_pid == getpid()`` proves the file is MINE, not a stale
   leftover of an earlier life). Any mismatch/absence/malformation is stale:
   the file is deleted and startup proceeds cold — the adapter-restart path,
   where the old tree is gone. Crash-recovery adoption of an orphaned tree is
   deliberately NOT implemented here; G4 builds on this module for that.
4. On a valid file the supervisor attaches every still-running unit (see
   ``adopt_child``) without spawning anything; units that are gone go through
   the normal start path, and the daemon deletes the file once the tree is up.

Window semantics: between the exec and the successor's socket rebind the K1
control plane refuses connections for well under a second — the listener fd
closes at exec, the successor unlinks the stale socket file and rebinds.
Clients should treat a refused connection during an upgrade as this rebind
window, not as a dead tree.

What does NOT cross the boundary: any in-memory counter. ``restart_count``,
``failure_streak`` and pending backoff timers are per-generation state and
start over for the successor (deliberate, recorded at W1.2c review); a unit
mid-backoff at handoff time simply takes the start path once.

The steady-state assumption behind an upgrade — no rolling replacement in
flight — is the orchestration layer's responsibility; this module neither
enforces nor repairs it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast

from services.ava_root.manifest import DesiredState

_log = logging.getLogger(__name__)

_HANDOFF_NAME = "handoff.json"
_HANDOFF_VERSION = 1
_UNIT_FIELDS = frozenset({"id", "desired", "pid", "pgid", "started_at"})
_FILE_FIELDS = frozenset({"version", "writer_pid", "written_at", "written_monotonic", "units"})


class HandoffError(ValueError):
    """The handoff file deviates from the closed format (fail-fast; no guessing)."""


class ChildState(StrEnum):
    """What a non-blocking ``waitpid`` probe found out about a pid."""

    LIVE = "live"
    """A live child of this process, still running."""
    EXITED = "exited"
    """A child that already exited; the probe reaped it (returncode carried)."""
    NOT_A_CHILD = "not-a-child"
    """No such child of this process (gone, reaped elsewhere, or never ours)."""


class ChildProbe(NamedTuple):
    """Result of :func:`probe_child`."""

    state: ChildState
    returncode: int | None
    """Exit code for ``EXITED`` (negative = signal); None otherwise."""


def handoff_path(run_dir: Path) -> Path:
    """The handoff file's location in a run directory."""
    return run_dir / _HANDOFF_NAME


@dataclass(frozen=True, slots=True)
class HandoffUnit:
    """One unit's carried state; pid/pgid/started_at are set for live units only."""

    unit_id: str
    desired: DesiredState
    pid: int | None = None
    pgid: int | None = None
    started_at: float | None = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.unit_id,
            "desired": self.desired.value,
            "pid": self.pid,
            "pgid": self.pgid,
            "started_at": self.started_at,
        }


@dataclass(frozen=True, slots=True)
class HandoffFile:
    """The whole handoff: every unit's desired state + the live pids at handoff time."""

    writer_pid: int
    written_at: str
    written_monotonic: float
    units: tuple[HandoffUnit, ...]

    @classmethod
    def stamp(cls, writer_pid: int, units: tuple[HandoffUnit, ...]) -> HandoffFile:
        """Build a handoff stamped with now (wall clock + monotonic)."""
        return cls(
            writer_pid=writer_pid,
            written_at=datetime.now(UTC).isoformat(),
            written_monotonic=time.monotonic(),
            units=units,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": _HANDOFF_VERSION,
            "writer_pid": self.writer_pid,
            "written_at": self.written_at,
            "written_monotonic": self.written_monotonic,
            "units": [unit.to_mapping() for unit in self.units],
        }


def write_handoff(run_dir: Path, handoff: HandoffFile) -> Path:
    """Write the handoff atomically (temp file + rename); returns its path."""
    path = handoff_path(run_dir)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(handoff.to_mapping(), indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return path


def discard_handoff(run_dir: Path) -> None:
    """Remove the handoff file if present (post-consume cleanup / stale purge)."""
    handoff_path(run_dir).unlink(missing_ok=True)


def load_handoff(
    run_dir: Path,
    *,
    expected_writer_pid: int,
    takeover_marker: bool,
) -> HandoffFile | None:
    """Load the handoff for adoption by THIS process; None means cold start.

    The file is accepted only when ``writer_pid == expected_writer_pid`` (exec
    keeps the pid). Every other outcome — absent, unreadable, malformed, or a
    foreign writer pid — is treated as stale: the file is removed, the reason
    is logged, and the caller cold-starts. ``takeover_marker`` (the successor's
    environment marker) only sharpens the logging: a marked process that finds
    no valid file is anomalous and warns loudly.
    """
    path = handoff_path(run_dir)
    if not path.exists():
        if takeover_marker:
            _log.warning("takeover marker is set but %s is missing; cold start", path)
        return None
    try:
        handoff = _parse(path)
    except (OSError, HandoffError) as exc:
        _log.warning("discarding handoff %s: %s", path, exc)
        discard_handoff(run_dir)
        return None
    if handoff.writer_pid != expected_writer_pid:
        _log.info(
            "handoff %s is stale (writer pid %s, this pid %s); cold start",
            path,
            handoff.writer_pid,
            expected_writer_pid,
        )
        discard_handoff(run_dir)
        return None
    _log.info(
        "handoff accepted: %d unit(s) carried from generation %s",
        len(handoff.units),
        handoff.writer_pid,
    )
    return handoff


def _parse(path: Path) -> HandoffFile:
    raw_text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise HandoffError(f"not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise HandoffError("top level must be an object")
    document = cast("dict[str, object]", raw)
    unknown = set(document) - _FILE_FIELDS
    if unknown:
        raise HandoffError(f"unknown field(s) {sorted(unknown)}; the format is closed")
    missing = _FILE_FIELDS - set(document)
    if missing:
        raise HandoffError(f"missing field(s) {sorted(missing)}")
    version = document["version"]
    if version != _HANDOFF_VERSION:
        raise HandoffError(f"unsupported version {version!r}")
    writer_pid = _require_int(document, "writer_pid", minimum=1)
    written_at = document["written_at"]
    if not isinstance(written_at, str) or not written_at:
        raise HandoffError("written_at must be a non-empty string")
    written_monotonic = _require_number(document, "written_monotonic")
    units_raw = document["units"]
    if not isinstance(units_raw, list):
        raise HandoffError("units must be a list")
    units: list[HandoffUnit] = []
    seen: set[str] = set()
    for index, item in enumerate(cast("list[object]", units_raw)):
        unit = _parse_unit(item, origin=f"units[{index}]")
        if unit.unit_id in seen:
            raise HandoffError(f"duplicate unit id {unit.unit_id!r}")
        seen.add(unit.unit_id)
        units.append(unit)
    return HandoffFile(
        writer_pid=writer_pid,
        written_at=written_at,
        written_monotonic=written_monotonic,
        units=tuple(units),
    )


def _parse_unit(item: object, *, origin: str) -> HandoffUnit:
    if not isinstance(item, dict):
        raise HandoffError(f"{origin}: must be an object")
    entry = cast("dict[str, object]", item)
    unknown = set(entry) - _UNIT_FIELDS
    if unknown:
        raise HandoffError(f"{origin}: unknown field(s) {sorted(unknown)}")
    unit_id = entry.get("id")
    if not isinstance(unit_id, str) or not unit_id:
        raise HandoffError(f"{origin}: id must be a non-empty string")
    desired_raw = entry.get("desired")
    try:
        desired = DesiredState(cast("str", desired_raw))
    except (TypeError, ValueError) as exc:
        raise HandoffError(f"{origin}: desired {desired_raw!r} is not a known state") from exc
    pid = entry.get("pid")
    pgid = entry.get("pgid")
    started_at = entry.get("started_at")
    if pid is None:
        if pgid is not None or started_at is not None:
            raise HandoffError(f"{origin}: pgid/started_at need a pid")
        return HandoffUnit(unit_id=unit_id, desired=desired)
    if not isinstance(pid, int) or pid <= 0:
        raise HandoffError(f"{origin}: pid must be a positive integer or null")
    if pgid is not None and (not isinstance(pgid, int) or pgid <= 0):
        raise HandoffError(f"{origin}: pgid must be a positive integer or null")
    if not isinstance(started_at, (int, float)) or started_at <= 0:
        raise HandoffError(f"{origin}: started_at must be a positive number for a live unit")
    return HandoffUnit(
        unit_id=unit_id,
        desired=desired,
        pid=pid,
        pgid=pgid,
        started_at=float(started_at),
    )


def _require_int(document: dict[str, object], field: str, *, minimum: int) -> int:
    value = document[field]
    if not isinstance(value, int) or value < minimum:
        raise HandoffError(f"{field} must be an integer >= {minimum}")
    return value


def _require_number(document: dict[str, object], field: str) -> float:
    value = document[field]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise HandoffError(f"{field} must be a number")
    return float(value)


def probe_child(pid: int) -> ChildProbe:
    """Non-blocking probe: is `pid` a live child, an exited one, or neither?

    An ``EXITED`` result reaps the child as a side effect (``waitpid`` without
    ``WNOHANG`` would block; with it, an exited child is returned immediately).
    """
    try:
        waited, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return ChildProbe(ChildState.NOT_A_CHILD, None)
    if waited == 0:
        return ChildProbe(ChildState.LIVE, None)
    return ChildProbe(ChildState.EXITED, os.waitstatus_to_exitcode(status))


class ProcessHandle(Protocol):
    """The slice of ``asyncio.subprocess.Process`` a unit generation needs.

    Both a normally spawned process and an :class:`AdoptedChild` satisfy this
    surface, so the supervisor's watch/stop paths are unchanged by adoption.
    """

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    def wait(self) -> Coroutine[Any, Any, int]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class AdoptedChild:
    """A still-running child inherited across the root's exec replacement.

    The exec keeps both the pid and the parent/child relation, so the child is
    still this process's to wait for — but the asyncio loop that originally
    spawned it is gone. A dedicated daemon thread blocks in ``waitpid`` and
    hands the exit back to the loop, which is exactly the standard library's
    own threading child-watcher strategy, scoped to one inherited child.

    Call only from a thread with a running asyncio loop (the supervisor's
    startup path); ``wait()`` is awaitable any number of times.
    """

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._returncode: int | None = None
        self._loop = asyncio.get_running_loop()
        self._exited: asyncio.Future[int] = self._loop.create_future()
        thread = threading.Thread(
            target=self._reap,
            name=f"ava-root-adopt-{pid}",
            daemon=True,
        )
        thread.start()

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        """Wait until the child exits; resolves with its returncode."""
        return await asyncio.shield(self._exited)

    def terminate(self) -> None:
        """SIGTERM the child; a no-op when it is already gone."""
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        """SIGKILL the child; a no-op when it is already gone."""
        self._signal(signal.SIGKILL)

    def _signal(self, signum: int) -> None:
        with suppress(ProcessLookupError):
            os.kill(self._pid, signum)

    def _reap(self) -> None:
        """Thread body: block in waitpid, then hand the exit to the loop."""
        try:
            _, status = os.waitpid(self._pid, 0)
            returncode = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            # Unreachable in the adopt path (the child was probed LIVE and the
            # relation is the exec's to keep); if it ever happens, fail loudly
            # and bias to failure so the restart policy still applies.
            _log.error("adopted child %s vanished before waitpid; treating as failed", self._pid)
            returncode = 1
        self._returncode = returncode
        try:
            self._loop.call_soon_threadsafe(self._exited.set_result, returncode)
        except RuntimeError:
            # The loop closed during shutdown; nobody is left to notify.
            _log.debug("adopted child %s exit %s: loop already closed", self._pid, returncode)


def adopt_child(pid: int) -> AdoptedChild:
    """Start re-attaching to an inherited live child; call from the loop thread."""
    return AdoptedChild(pid)
