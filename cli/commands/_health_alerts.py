"""Health-probe alert machinery: failure counting, edge alerts, auto-rollback gate.

Split out of ``cli.commands._cluster_health`` (2026-08-07, Task #1025) to keep
that module under the per-file 800-line ceiling once the non-prod-checkout
guard (PR #1821) and R2-D's deploy-window changes (PR #1824) both landed.

Owns the consecutive-failure counter file, the edge-triggered owner alert
(W16, via the IM bridge /send RPC with the alerts ingest), the local
fallback ingest path, and the auto-rollback trigger at the failure threshold.
The probe runner itself (`run_health_probe`) stays in ``_cluster_health`` and
imports the pieces it needs from here.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

# Consecutive-failure tracking file. Lives under $AVA_HOME so it is
# cluster-scoped and survives restarts. Its four lines are count, failure
# class, reason, and timestamp; it deliberately has no DB dependency because
# the probe might be running when the DB is down.
FAILURE_COUNT_FILE = "health_probe_failures"

# Edge-trigger state for owner alerts: holds the failure message of the alert
# last sent plus the instance's starts_at timestamp (one ISO-8601 line each);
# absent = last run was healthy. Same $AVA_HOME placement rationale as
# FAILURE_COUNT_FILE. The starts_at line is the alerts instance key — the
# recovery edge must reference the exact (fingerprint, starts_at) row the
# firing edge created, so the two flips resolve as one alert instance.
ALERT_STATE_FILE = "health_probe_alert"


def _notify_owner(text: str) -> None:
    """Best-effort push to the owner through the im_bridge daemon's `/send` RPC.

    The IM Bridge (services/im_bridge) is the only Telegram frontend — the user
    ruling in decisions/2026-08-03-telegram-skill-removed.md — and it is
    also the framework-owned owner-notification channel: the daemon fans one
    message out to every loaded IM adapter (Telegram / WeChat / Feishu) via
    `IMBridgeCore.notify_user`, so the owner hears on whichever channels are
    actually connected.

    Since W16 this is the *fallback* path only: the normal edge alert goes
    through the gateway's alerts ingest (which fans IM out itself — one
    alert = one IM, no double-send), and this function covers the corners the
    ingest cannot reach: the gateway unreachable AND its database down, a
    pre-W16 state file with no persisted instance, and a recovery the ingest
    silently inserted for an instance it never saw fire. In those corners the
    probe POSTs the local daemon's health-port `/send` route directly —
    through the gateway (which may be the thing that is down) is exactly what
    must be avoided — the same independence the old direct Bot API call had,
    now through the sanctioned channel. The only remaining dependency is the
    loopback health port of a daemon on the gateway host.

    Every alert is stamped with the cluster name (`[<cluster>] ...`) so the
    owner can tell which cluster is talking at a glance — a preview cluster's
    alert must not read like a prod incident.

    No-ops (logs) when IM notifications are disabled (`alerts.im_notify_enabled`
    — the same master switch the gateway's alerts ingest honours). Never
    raises: alerting is a side channel and must not break the probe or the
    auto-rollback path it gates; a delivery failure (im_bridge down, network
    error) is logged to stderr and otherwise surfaces only through the probe's
    exit code in the cron log.

    Delivery semantics (R2 design Q1 — ruling pending, kept as-is per design
    §7 option B): at-most-once, never silent — failure is logged to stderr.
    The R3 migration shape is ``Policy(max_attempts=1, idempotent=False,
    on_final_failure=log)``."""
    from shared.cluster import home_label
    from shared.config import settings
    from shared.daemon_health import health_port
    from shared.paths import ava_home

    if not settings.alerts.im_notify_enabled:
        print("  (owner alert skipped: IM notifications disabled)", file=sys.stderr)
        return

    base = (settings.services.im_bridge_health_url or "").rstrip("/") or (
        f"http://127.0.0.1:{health_port('im_bridge')}"
    )
    stamped = f"[{home_label(ava_home())}] {text}"
    try:
        resp = httpx.post(
            f"{base}/send",
            json={"text": stamped},
            headers={"Authorization": f"Bearer {settings.data_plane.cluster_secret}"},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Log only the status code + the daemon's error body. Never format the
        # exception itself: httpx's repr embeds the request, and the request's
        # Authorization header must not leak into the cron log.
        print(
            f"  (owner alert delivery failed: HTTP {e.response.status_code} {e.response.text[:200]})",
            file=sys.stderr,
        )
    except Exception as e:
        # Transport errors (timeout / connection — the daemon may be down) —
        # log the class only, never the exception (which can carry the request).
        print(f"  (owner alert delivery failed: {type(e).__name__})", file=sys.stderr)


# The alerts identity of every health-probe alert. One row per outage
# instance: alertname is what the UI shows, severity is the alert class
# ('error' — the old P1 tier, the incident class the health probe alerts at).
# The (fingerprint, starts_at) dedup key is computed by the ingest.
OPS_RULE_UID = "health-probe"
OPS_RULE_NAME = "cluster health"
OPS_SEVERITY = "error"


def _alert_summary(*, recovered: bool, message: str) -> str:
    """The alert text as the panel + IM see it, stamped with the cluster name.

    Stamping in the ingest payload (not in the IM send) covers every channel
    uniformly: the gateway's IM fan-out and the ops panel row both carry the
    `[<cluster>]` prefix, so a preview cluster's alert never reads like a
    prod incident."""
    from shared.cluster import home_label
    from shared.paths import ava_home

    label = home_label(ava_home())
    if recovered:
        return f"[{label}] [health-probe] cluster recovered: all checks passing"
    return f"[{label}] [health-probe] cluster unhealthy: {message}"


def _ingest_alert(
    *, status: Literal["firing", "resolved"], message: str, starts_at: datetime
) -> None:
    """Push one health-probe edge alert through the alerts ingest pipeline.

    POSTs an Alertmanager-webhook-shaped payload (with ``source="health-probe"``)
    to the gateway's ``/api/alerts`` — the single funnel that
    stores the row AND fans the IM notification out (one alert = one row +
    one IM). The probe never calls im_bridge itself on this path; that is the
    ingest's job (W16, see decisions/2026-08-03-telegram-skill-removed.md
    for why im_bridge is the only sanctioned surface).

    ``starts_at`` is the instance key: the firing edge generates it and stores
    it in ALERT_STATE_FILE, and the recovery edge replays it so both flips
    resolve to the same (fingerprint, starts_at) row.

    Never raises. On a transport/HTTP failure the gateway is unreachable —
    typically the very outage being reported — so the probe falls back to
    writing the row and sending the IM itself (`_ingest_alert_fallback`);
    alerting is a side channel and must never break the probe or the
    auto-rollback path it gates.
    """
    from shared.config import settings

    summary = _alert_summary(recovered=status == "resolved", message=message)
    payload = {
        "source": "health-probe",
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": OPS_RULE_NAME,
                    "severity": OPS_SEVERITY,
                },
                "annotations": {"summary": summary},
                "startsAt": starts_at.isoformat(),
                "endsAt": "" if status == "firing" else datetime.now(UTC).isoformat(),
            }
        ],
    }
    try:
        from shared.machine import gateway_api_base

        resp = httpx.post(
            f"{gateway_api_base()}/api/alerts",
            json=payload,
            headers={"Authorization": f"Bearer {settings.data_plane.cluster_secret}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        _ingest_recovery_self_heal(resp, status=status)
    except httpx.HTTPStatusError as e:
        print(
            f"  (health alert ingest failed: HTTP {e.response.status_code} "
            f"{e.response.text[:200]} — falling back to local ingest)",
            file=sys.stderr,
        )
        _ingest_alert_fallback(status=status, message=message, starts_at=starts_at)
    except Exception as e:
        # Transport errors (timeout / connection refused) and a missing
        # gateway URL — log the class only, never the exception (which can
        # carry the Authorization header).
        print(
            f"  (health alert ingest failed: {type(e).__name__} — falling back to local ingest)",
            file=sys.stderr,
        )
        _ingest_alert_fallback(status=status, message=message, starts_at=starts_at)


def _ingest_recovery_self_heal(resp: httpx.Response, *, status: str) -> None:
    """Close the one corner the ingest cannot: a recovery for an instance the
    gateway never saw fire.

    When the firing edge had to fall back to IM-only (gateway AND database
    were both down), no row exists for the resolved POST to flip — the ingest
    inserts a fresh resolved row and, by its transition gate, stays silent.
    The owner heard the firing (last-resort IM) and would never hear the
    recovery. A 200 with ``inserted=1`` + ``notified=0`` on a resolved edge is
    exactly that signature, so the probe sends the recovery note directly.
    Any other response shape is the ingest's normal business — the probe does
    not second-guess a 2xx beyond this bounded case.
    """
    if status != "resolved":
        return
    try:
        body = resp.json()
    except Exception:
        return
    if body.get("inserted") == 1 and body.get("notified") == 0:
        _notify_owner("[health-probe] cluster recovered: all checks passing")


def _ingest_alert_fallback(
    *, status: Literal["firing", "resolved"], message: str, starts_at: datetime
) -> None:
    """Gateway unreachable — run the ingest logic locally (same code the
    gateway's endpoint uses) so the alert still lands: one alerts row +
    one IM, exactly as the normal path would have produced.

    The anti-double guard rides on the ingest's own transition gate plus the
    row's notified_at: a re-run of this fallback (or a gateway that processed
    the POST but lost the response) must not send a second IM. If the
    database is down too, degrade to the legacy direct-IM path — the owner
    still hears, which matters more than the row when the UI is dark too.
    """
    import shared.db
    from shared.alerts import display_language, notify_im, notify_text, stamp_notified, upsert_alert

    # The Alertmanager-webhook alert shape as a plain dict — the same payload
    # the gateway ingest would have parsed (model_dump keys), so the shared
    # core treats this run exactly like the HTTP path.
    alert: dict[str, Any] = {
        "status": status,
        "labels": {
            "alertname": OPS_RULE_NAME,
            "severity": OPS_SEVERITY,
        },
        "annotations": {"summary": _alert_summary(recovered=status == "resolved", message=message)},
        "starts_at": starts_at.isoformat(),
        "ends_at": "" if status == "firing" else datetime.now(UTC).isoformat(),
    }
    try:
        with shared.db.connect() as conn:
            text = notify_text(alert, display_language(conn))
            key, did_insert, should_notify, row = upsert_alert(conn, alert, source="health-probe")
            notified_at = row.get("notified_at")
            # Gate: the ingest's own transition decision, plus — when the
            # gateway may have processed the POST but lost the response —
            # "not stamped yet" (the firing edge whose IM never landed must
            # still be sent; a resolved edge for an instance that never fired
            # must not).
            if should_notify or (notified_at is None and not did_insert):
                notified_ok = notify_im(text)
                if notified_ok:
                    stamp_notified(conn, [key])
            conn.commit()
    except Exception as e:
        print(
            f"  (health alert local ingest failed: {type(e).__name__} — direct IM only)",
            file=sys.stderr,
        )
        _notify_owner(
            f"[health-probe] cluster {'recovered: all checks passing' if status == 'resolved' else f'unhealthy: {message}'}"
        )


def _alert_failure(home: Path, message: str) -> None:
    """Owner alert on the healthy->unhealthy edge (or when the failure reason
    changes, e.g. a dead frontend escalating to a dead gateway). Repeat runs
    failing with the same message stay silent — the probe fires every few
    minutes and must not turn a persistent outage into a notification storm.

    The edge also fixes the alert instance's starts_at into the state file:
    the recovery edge replays it so both flips resolve to one alerts row
    (fingerprint x starts_at dedup key)."""
    marker = home / ALERT_STATE_FILE
    if marker.exists():
        lines = marker.read_text().splitlines()
        # The failure message may itself contain newlines; the starts_at key
        # is always the LAST line, so the dedup comparison is the text before
        # it (audit P3: lines[0] broke dedup + starts_at parsing on
        # multi-line messages).
        if lines and "\n".join(lines[:-1]) == message:
            return
    starts_at = datetime.now(UTC)
    marker.write_text(f"{message}\n{starts_at.isoformat()}")
    _ingest_alert(status="firing", message=message, starts_at=starts_at)


def _alert_recovery(home: Path) -> None:
    """Owner alert on the unhealthy->healthy edge; no-op when the last run
    was already healthy (no state file)."""
    marker = home / ALERT_STATE_FILE
    if not marker.exists():
        return
    lines = marker.read_text().splitlines()
    marker.unlink()
    if len(lines) > 1:
        try:
            # starts_at is the LAST line — the message may contain newlines.
            starts_at = datetime.fromisoformat(lines[-1])
        except ValueError:
            starts_at = None
    else:
        starts_at = None
    if starts_at is not None:
        _ingest_alert(status="resolved", message="all checks passing", starts_at=starts_at)
    else:
        # Pre-W16 state file (failure message only, no instance key): no
        # alerts row exists to resolve, and the firing was IM'd directly
        # back then — so the recovery goes directly too.
        _notify_owner("[health-probe] cluster recovered: all checks passing")


def _reset_failure_count(home: Path) -> None:
    """Reset the consecutive-failure counter to 0."""
    (home / FAILURE_COUNT_FILE).write_text(f"0\ncode\n\n{datetime.now(UTC).isoformat()}")


def _increment_failure_count(home: Path, *, failure_class: str = "code", reason: str = "") -> int:
    """Increment the code-failure counter and retain its last classified reason."""
    counter_path = home / FAILURE_COUNT_FILE
    try:
        lines = counter_path.read_text().splitlines()
        current = int(lines[0]) if len(lines) == 4 else 0
    except (FileNotFoundError, ValueError):
        current = 0
    current += 1
    counter_path.write_text(
        f"{current}\n{failure_class}\n{reason}\n{datetime.now(UTC).isoformat()}"
    )
    return current


def _deploy_suppression() -> str | None:
    """The reason this failure must NOT advance the auto-rollback counter, or None.

    **A cluster mid-deploy is not an unhealthy cluster.** Every rollback-gating check
    here (gateway liveness, agent population, schema) fails *by design* while a deploy
    stops services and migrates — the expected state, not evidence the new code is
    bad. This probe is OS-scheduled, so it keeps firing throughout; at
    `StartInterval 300` with `--threshold 3`, fifteen minutes of a legitimate deploy
    is enough to auto-roll-back production out from under the rollout still running,
    with the two actors pulling the pin in opposite directions. The 2026-07-29
    rollout's first phase took ~8 minutes. That it did not trip was timing.

    This was the one automated actor that did not consult the deploy lease. The pin
    controller, the code controller and the stranded-pause controller all already ask
    "is a deploy running?" and defer; the probe did not, and nobody is watching when
    it fires.

    **Bounded by the lease's own TTL, with no second clock.** A deploy that dies
    holding the lease cannot silence the probe past that: the lease stops being live
    and failures count again. A settle hold is bounded harder still — `deploy_in_flight`
    releases it the moment every host reaches the pin, rather than waiting out the
    window. An unreadable lease does NOT suppress: the probe must not be talked out
    of its job by a Postgres hiccup, and "cannot prove a deploy is running" has to
    mean "assume none is" (`deploy_in_flight` returns not-active on any failure).

    **This is also why `--threshold` does not have to grow when the fleet gets
    slower.** The lease is what protects a deploy from this probe, and the lease is
    now shaped by the deploy (renewed while it runs, held over the hosts still
    converging — `shared.deploy_timing`), not by a duration guessed here. A threshold
    raised to cover the slowest imaginable rollout would only make a genuinely bad
    release live longer; the deploy window is the right instrument, and it is already
    exact.
    """
    from ops.deploy_window import deploy_in_flight

    window = deploy_in_flight()
    return window.detail if window.active else None


def _handle_consecutive_failure(
    home: Path,
    threshold: int,
    *,
    failure_class: str = "code",
    reason: str = "",
) -> None:
    """Record a probe failure and trigger rollback once failures reach the threshold.

    Bumps the consecutive-failure counter; when it reaches `threshold`, runs
    `ava cluster rollback --yes`. The counter is reset ONLY after a
    successful rollback, so a failed rollback is re-attempted on the next probe
    run rather than resetting the count and starting the countdown over.

    Only reached when no deploy is in flight — the caller gates on
    `_deploy_suppression`, which is where the reasoning for that lives.

    The rollback is invoked via `sys.argv[0]` — the absolute `ava` path the OS
    scheduler launched this probe with — not a bare `ava` on PATH, which would
    not resolve under launchd/cron's minimal PATH (and guarantees the rollback
    targets the same cluster as the probe)."""
    if failure_class != "code":
        return
    count = _increment_failure_count(home, failure_class=failure_class, reason=reason)
    if count < threshold:
        print(f"  consecutive failure {count}/{threshold}", file=sys.stderr)
        return

    print(
        f"  consecutive failures reached {count} (threshold {threshold}) — rolling back",
        file=sys.stderr,
    )
    result = subprocess.run(
        [sys.argv[0], "cluster", "rollback", "--yes"],
        check=False,
    )
    if result.returncode == 0:
        _reset_failure_count(home)
        print("  rollback succeeded — failure count reset", file=sys.stderr)
    else:
        print(
            f"  rollback failed (exit {result.returncode}) — failure count kept at {count}",
            file=sys.stderr,
        )
