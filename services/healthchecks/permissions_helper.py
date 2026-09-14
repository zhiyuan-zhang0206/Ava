"""Watchdog probe and launchd-state classifier for the macOS permissions helper.

One module serves the two lifecycle eras (task #3393):

- **Era 1 — the watchdog loop** (``main()``, one call per 60s round): ping the
  helper; on failure classify the launchd job state; report once per episode;
  repair persistent failures with a bounded bootout+bootstrap, and when a
  repair does not restore ping escalate through the unified event pipeline and
  retry under backoff — never silently sealing the episode (the F5 finding: an
  LWCR-stuck job does not heal itself).
- **Era 2 — the root probe** (``probe()``): the total verdict callable the
  root era registers through the wiring's static path. It detects and
  classifies, nothing else — the repair owner stays this module's era-1 loop
  and the helper's own lifecycle (CLI converge); the root health monitor turns
  the verdict into its health surface and breaker alert.

Classification (pure, fixture-tested) reads the ``job state`` line, never the
top-level ``state`` — a stuck job's ``state`` still reads ``spawn scheduled``
(F5 findings section 6; PR #2500 review). LWCR-class = ``spawn failed`` plus
(``last exit code`` beginning 78 or the ``needs LWCR update`` marker); the bare
78 is not LWCR (a missing app also exits 78). ``last exit code`` can lag the
failure by seconds: a bare spawn-failed round classifies as ``spawn-failed``
and the next round re-classifies — the lag costs one round, not a wrong alarm.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from services.permissions_helper import client
from services.permissions_helper.launchd_job import (
    HelperJobState,
    parse_job_state,
    read_helper_job,
)
from services.permissions_helper.lifecycle import repair_unresponsive_helper
from shared.daemon_health import DaemonProbe
from shared.log import init_gateway_process
from shared.paths import permissions_helper_socket
from shared.platform import IS_MACOS

_log = logging.getLogger("services.healthchecks.permissions_helper")

_PING_TIMEOUT_S = 3.0
_PING_RESPONSE_LIMIT = 64 * 1024
_REPAIR_AFTER_FAILURES = 3

# Repair-retry backoff (task #3393): the first failed repair retries after 5
# minutes, each further failure doubles the wait, capped at one hour. The
# episode keeps retrying — spaced by the backoff, never sealed.
_REPAIR_BACKOFF_BASE_S = 300.0
_REPAIR_BACKOFF_CAP_S = 3600.0

# Classification vocabulary (the values carried by logs and event fields).
HEALTHY = "healthy"
LWCR_STUCK = "lwcr-stuck"
SPAWN_FAILED = "spawn-failed"
UNRESPONSIVE = "unresponsive"
ABSENT = "absent"

# The watchdog imports this module once and calls main() every 60 seconds, so
# episode state stays in memory for the life of that long-running process.
_consecutive_failures: int = 0
_reported_unhealthy: bool = False
_repair_attempts: int = 0
_next_repair_at: float = 0.0


def _monotonic() -> float:
    """Wall-independent clock for the repair backoff — a seam tests can advance."""
    return time.monotonic()


def _ping() -> bool:
    """Ping the real helper protocol with a watchdog-sized response timeout."""
    sock = client._connect(str(permissions_helper_socket()))
    try:
        sock.settimeout(_PING_TIMEOUT_S)
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


def _clear_unhealthy_episode() -> None:
    global _consecutive_failures, _reported_unhealthy, _repair_attempts, _next_repair_at  # noqa: PLW0603

    _consecutive_failures = 0
    _reported_unhealthy = False
    _repair_attempts = 0
    _next_repair_at = 0.0


def _repair_backoff_s(attempts: int) -> float:
    """Delay armed after a failed repair: the base doubling per attempt, capped."""
    return min(_REPAIR_BACKOFF_BASE_S * (2 ** (attempts - 1)), _REPAIR_BACKOFF_CAP_S)


def main() -> None:
    """One watchdog round: classify, report once per episode, repair with escalation + backoff."""
    global _consecutive_failures, _reported_unhealthy, _repair_attempts, _next_repair_at  # noqa: PLW0603

    init_gateway_process(name="permissions_helper-healthcheck")
    if not IS_MACOS:
        return

    observation = _observe()
    if observation.classification == HEALTHY:
        _clear_unhealthy_episode()
        return

    _consecutive_failures += 1
    if not _reported_unhealthy:
        _reported_unhealthy = True
        _log.error("[permissions-helper healthcheck] helper unhealthy (%s)", observation.detail)
        _emit_unhealthy(observation)

    if _consecutive_failures < _REPAIR_AFTER_FAILURES:
        return
    now = _monotonic()
    if now < _next_repair_at:
        return

    _repair_attempts += 1
    delay_s = _repair_backoff_s(_repair_attempts)
    _next_repair_at = now + delay_s
    try:
        repaired = repair_unresponsive_helper()
    except Exception:
        _log.error(
            "[permissions-helper healthcheck] launchd repair raised (attempt %d)",
            _repair_attempts,
            exc_info=True,
        )
        _emit_repair_failed(observation, attempt=_repair_attempts, retry_s=delay_s)
        return
    if repaired:
        _log.warning(
            "[permissions-helper healthcheck] launchd repair answered ping; "
            "the next watchdog round will verify recovery"
        )
        return
    _log.error(
        "[permissions-helper healthcheck] launchd repair did not restore ping (attempt %d) — "
        "retrying in %ds under backoff; manual intervention may be needed",
        _repair_attempts,
        int(delay_s),
    )
    _emit_repair_failed(observation, attempt=_repair_attempts, retry_s=delay_s)


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


def _emit_repair_failed(observation: _Observation, *, attempt: int, retry_s: float) -> None:
    """One registered event per failed repair attempt — the escalation path."""
    from shared.log import logger

    logger.error(
        "[permissions-helper healthcheck] launchd repair failed (attempt {attempt})",
        event="permissions_helper_repair_failed",
        attempt=attempt,
        retry_s=int(retry_s),
        classification=observation.classification,
        detail=observation.detail,
        **_job_fields(observation),
    )


def probe() -> DaemonProbe:
    """The root-era verdict (task #3393): detect and classify, never act.

    Registered by the root wiring's static path on macOS hosts with the helper
    enabled (``services.ava_root_glue.glue``). Total by contract — the runner
    must never see this raise. The repair for what this verdict detects belongs
    to the era-1 loop above and to the helper's own lifecycle (CLI converge);
    the root monitor only reports.
    """
    if not IS_MACOS:
        return DaemonProbe.up("not macOS: no launchd-owned permissions helper")
    try:
        observation = _observe()
    except Exception as exc:
        return DaemonProbe.down(f"probe error: {type(exc).__name__}: {exc}")
    if observation.classification == HEALTHY:
        return DaemonProbe.up("ping answered; helper serving")
    return DaemonProbe.down(observation.detail)


if __name__ == "__main__":
    main()
