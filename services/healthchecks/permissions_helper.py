"""Read-only macOS parent-helper ping and launchd-state classification.

The root cannot restart its own permission ancestor. Failed episodes require an
external transition to settle custody before helper replacement.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass

import psutil

from services.permissions_helper import client
from services.permissions_helper.launchd_job import (
    HelperJobState,
    parse_job_state,
    read_helper_job,
)
from shared.daemon_health import DaemonProbe
from shared.paths import permissions_helper_socket
from shared.platform import IS_MACOS
from shared.proc_tree import OwnedProcess

_PING_TIMEOUT_S = 3.0
_PING_RESPONSE_LIMIT = 64 * 1024
# Classification vocabulary (the values carried by logs and event fields).
HEALTHY = "healthy"
LWCR_STUCK = "lwcr-stuck"
SPAWN_FAILED = "spawn-failed"
UNRESPONSIVE = "unresponsive"
ABSENT = "absent"

_reported_unhealthy: bool = False


class _ParentEvidenceError(RuntimeError):
    """The observer cannot bind the helper endpoint to the captured root parent."""


def _helper_parent(sock: socket.socket) -> tuple[OwnedProcess, OwnedProcess]:
    try:
        return _read_helper_parent(sock)
    except Exception as exc:
        raise _ParentEvidenceError(f"helper parent evidence unavailable: {exc}") from exc


def _read_helper_parent(sock: socket.socket) -> tuple[OwnedProcess, OwnedProcess]:
    from services.ava_root.client import root_process
    from services.healthchecks.owned_service import _peer_pid

    root = root_process()
    if root is None:
        raise _ParentEvidenceError("no captured root generation")
    parent = psutil.Process(root.pid).parent()
    if parent is None:
        raise _ParentEvidenceError("root has no observable native parent")
    owner = OwnedProcess.capture(parent)
    if _peer_pid(sock) != owner.pid or not root.live() or not owner.live():
        raise _ParentEvidenceError("helper peer is not the captured root parent")
    return root, owner


def _parent_still_live(root: OwnedProcess, parent: OwnedProcess, reply_pid: int) -> bool:
    try:
        return (
            reply_pid == parent.pid
            and root.live()
            and parent.live()
            and psutil.Process(root.pid).ppid() == parent.pid
        )
    except psutil.Error as exc:
        raise _ParentEvidenceError(f"cannot recheck helper parent: {exc}") from exc


def _ping() -> bool:
    """Ping the real helper protocol with a watchdog-sized response timeout."""
    sock = client._connect(str(permissions_helper_socket()))
    try:
        sock.settimeout(_PING_TIMEOUT_S)
        root, parent = _helper_parent(sock)
        sock.sendall((json.dumps({"id": 0, "method": "ping"}) + "\n").encode())
        reply = bytearray()
        while not reply.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply.extend(chunk)
            if len(reply) > _PING_RESPONSE_LIMIT:
                raise client.PermissionsHelperError(
                    "permissions helper ping response exceeded line limit"
                )
    finally:
        sock.close()

    result: client.PingResult = client._parse_reply(bytes(reply), "ping")
    if "pid" not in result or not _parent_still_live(root, parent, result["pid"]):
        raise _ParentEvidenceError("helper reply lost captured parent identity")
    return result["pong"] is True


@dataclass(frozen=True, slots=True)
class _Observation:
    """One round's view of the helper: ping result plus launchd classification."""

    classification: str
    detail: str
    job: HelperJobState | None = None


def _classify(*, ping_ok: bool, job: HelperJobState | None) -> str:
    """The LWCR truth table (pure): one failure class per observation.

    ``job`` is None when launchd has no readable job. The LWCR class needs the
    spawn-failed ``job state`` plus one LWCR signal — the ``needs LWCR update``
    marker or the 78 exit code; a bare 78 without spawn-failed is not one.
    """
    if ping_ok:
        return HEALTHY
    if job is None:
        return ABSENT
    if job.job_state == "spawn failed":
        exit_code = job.last_exit_code or ""
        if job.needs_lwcr_update or exit_code.startswith("78"):
            return LWCR_STUCK
        return SPAWN_FAILED
    return UNRESPONSIVE


def _detail(classification: str, job: HelperJobState | None, ping_failure: str | None) -> str:
    """One greppable line: the classification plus the launchd facts behind it."""
    parts = [classification]
    if job is not None:
        if job.job_state is not None:
            parts.append(f"job state={job.job_state}")
        if job.last_exit_code is not None:
            parts.append(f"last exit={job.last_exit_code}")
        if job.needs_lwcr_update:
            parts.append("needs LWCR update")
        if job.btm_uuid is not None:
            parts.append(f"BTM uuid={job.btm_uuid}")
        if job.runs is not None:
            parts.append(f"runs={job.runs}")
    if ping_failure is not None:
        parts.append(ping_failure)
    return "; ".join(parts)


def _observe() -> _Observation:
    """Ping the helper; on failure read and classify the launchd job (total)."""
    try:
        ping_ok = _ping()
    except _ParentEvidenceError:
        raise
    except Exception as exc:
        ping_ok = False
        ping_failure: str | None = f"{type(exc).__name__}: {exc}"
    else:
        ping_failure = None if ping_ok else "pong was not true"
    if ping_ok:
        return _Observation(HEALTHY, "ping answered")
    job = None
    text = read_helper_job()
    if text is not None:
        job = parse_job_state(text)
    classification = _classify(ping_ok=False, job=job)
    return _Observation(classification, _detail(classification, job, ping_failure), job)


def report(result: DaemonProbe) -> None:
    """Record one observed unhealthy episode; recovery rearms reporting."""
    global _reported_unhealthy  # noqa: PLW0603
    if result.alive:
        _reported_unhealthy = False
    elif not _reported_unhealthy:
        _reported_unhealthy = True
        _emit_unhealthy(_Observation(result.detail.split(";", 1)[0], result.detail))


def _job_fields(observation: _Observation) -> dict[str, object]:
    """The launchd facts as structured event fields (None when unread)."""
    job = observation.job
    return {
        "job_state": None if job is None else job.job_state,
        "last_exit_code": None if job is None else job.last_exit_code,
        "needs_lwcr_update": None if job is None else job.needs_lwcr_update,
        "btm_uuid": None if job is None else job.btm_uuid,
    }


def _emit_unhealthy(observation: _Observation) -> None:
    """One registered event per episode — the report the operator surfaces inherit."""
    from shared.log import logger

    logger.error(
        "[permissions-helper healthcheck] helper unhealthy ({classification})",
        event="permissions_helper_unhealthy",
        classification=observation.classification,
        detail=observation.detail,
        **_job_fields(observation),
    )


def probe() -> DaemonProbe:
    """Detect and classify; inspection failures remain unavailable evidence."""
    if not IS_MACOS:
        return DaemonProbe.up("not macOS: no launchd-owned permissions helper")
    try:
        observation = _observe()
    except Exception as exc:
        return DaemonProbe.unavailable(f"probe error: {type(exc).__name__}: {exc}")
    if observation.classification == HEALTHY:
        return DaemonProbe.up("ping answered; helper serving")
    return DaemonProbe.down(observation.detail)
