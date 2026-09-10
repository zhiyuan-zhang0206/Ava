"""Best-effort operator alert when an exec child dies before running agent code.

The bootstrap-class exec failure (P2 #2102): the child crashed during boot —
most often because GET /api/bootstrap was unreachable while the gateway was
down — so the agent's code never ran. That is exactly the class that used to
surface as a bare "(no output)" lie; the honest envelope (P0 #2100) is the
agent-facing half of the fix, this alert is the operator-facing half.

Posts through the gateway's /api/alerts ingest — the single funnel that stores
the row AND fans the IM notification out (same shape the health probe posts,
W16) — with the cluster secret as the bearer credential. Never raises and
never blocks the agent's event loop: the POST runs on a daemon thread, and a
per-process rate limit turns an outage that fails every exec into one alert
per window instead of one per attempt. When the gateway itself is unreachable
the POST just fails silently — the machine-level health alert already covers
that outage.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from shared.log import logger

# One bootstrap-failure alert per process per window: an outage fails every
# exec, and the point is one visible signal (plus a slow reminder), not a row
# per execute_code attempt.
_ALERT_RATE_LIMIT_S = 600.0

_ALERT_NAME = "exec child boot failed"
_SEVERITY = "warning"
_SOURCE = "agent-exec"
_POST_TIMEOUT_S = 10.0

_last_posted_at: float | None = None
_rate_lock = threading.Lock()


def _payload(agent_id: int, exc_type: str, exc_msg: str) -> dict[str, Any]:
    """Alertmanager-webhook-shaped payload (one alert instance, firing)."""
    from shared.machine import machine_name

    labels = {
        "alertname": _ALERT_NAME,
        "severity": _SEVERITY,
        "agent": str(agent_id),
        "machine": machine_name(),
    }
    return {
        "source": _SOURCE,
        "alerts": [
            {
                "status": "firing",
                "labels": labels,
                "annotations": {
                    "summary": (
                        f"agent {agent_id} ({labels['machine']}): exec child crashed "
                        f"before running code ({exc_type}): {exc_msg[:200]}"
                    )
                },
                "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "endsAt": "",
                "fingerprint": f"exec-child-boot-failed:{labels['machine']}:{agent_id}",
            }
        ],
    }


def _post(agent_id: int, exc_type: str, exc_msg: str) -> None:
    """Run on a daemon thread: POST the alert, swallowing every failure."""
    try:
        import httpx

        from shared.config import settings
        from shared.machine import gateway_api_base

        resp = httpx.post(
            f"{gateway_api_base()}/api/alerts",
            json=_payload(agent_id, exc_type, exc_msg),
            headers={"Authorization": f"Bearer {settings.data_plane.cluster_secret}"},
            timeout=_POST_TIMEOUT_S,
        )
        if resp.status_code >= 400:
            logger.warning(
                "[exec-alert] bootstrap-failure alert ingest declined (HTTP %s) for agent %s",
                resp.status_code,
                agent_id,
            )
    except Exception as exc:  # transport / config errors — never raise from a thread
        logger.warning(
            "[exec-alert] could not post bootstrap-failure alert for agent %s: %s",
            agent_id,
            type(exc).__name__,
        )


def maybe_alert_exec_boot_failure(agent_id: int, exc: BaseException) -> None:
    """Rate-limited, non-blocking alert for a boot-phase exec child crash.

    Called from the exec dispatcher when the child's envelope reports
    code_reached=False. Returns immediately; the actual HTTP POST happens on a
    daemon thread so a slow or dead gateway cannot stall the agent's turn.
    """
    global _last_posted_at  # noqa: PLW0603
    now = time.monotonic()
    with _rate_lock:
        if _last_posted_at is not None and now - _last_posted_at < _ALERT_RATE_LIMIT_S:
            return
        _last_posted_at = now
    exc_type = getattr(exc, "exc_type", None) or type(exc).__name__
    exc_msg = getattr(exc, "exc_msg", None) or str(exc)
    threading.Thread(
        target=_post,
        args=(agent_id, exc_type, exc_msg),
        name=f"exec-boot-alert-{agent_id}",
        daemon=True,
    ).start()
