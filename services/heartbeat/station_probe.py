"""Remote observatory-station probe — run every 60s by the GATEWAY watchdog.

Lives in services/heartbeat/ (not services/healthchecks/) on purpose: it consumes the
`alerts` settings domain, which is gateway-owned — the runner watchdog imports the
healthcheck roster too, so a module under services/healthchecks/ would drag the alerts
domain into the runner profile (test_gateway_consumer_guard). The watchdog resolves it
by dotted string, so the runner closure never contains it.

The GATEWAY's probe of a REMOTE observatory station (WP4, task #1946;
conventions/reachability-and-credentials.md). When `AVA_OBSERVABILITY_URL`
is empty the check is a no-op: the observatory is local and the `lgtm`
healthcheck keeps the native stack alive. When it is set, the gateway dials
the station through the reachability contract — the address the station
unit advertises in `machine_units` (`base.cluster.machines.unit_dial_url`), not a
bare connect — and authenticates with the cluster's telemetry token, exactly
like the collector relay that ships telemetry to it.

The probe is an OTLP round-trip: `POST <advertised url>/v1/traces` with an
empty `ExportTraceServiceRequest` and `Authorization: Bearer <telemetry token>`. Any
2xx counts as alive (the station's `otlp/remote` receiver authenticates and
accepts the empty batch); a connection failure, timeout, 401, or 4xx/5xx
means the station's ingress is not serving.

**Fail-open by design**: a failed probe NEVER blocks, restarts, or sheds
local business — the gateway's collector keeps buffering in its file-backed
queue and the local stack is untouched. The probe only records an alert
("observatory station offline", consecutive-failure gated like the machine
offline probe) and resolves it on recovery. Every failure path is caught and
logged; main() never raises.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from base.config import settings
from base.db import Database
from base.deploy.transition import transition_severity
from base.log import init_gateway_process, logger
from base.telemetry.station_endpoint import StationTarget as _StationTarget
from base.telemetry.station_endpoint import resolve_station_target, validated_observability_base

_log = logging.getLogger("services.heartbeat.station_probe")

# Consecutive failed probes before the alert fires. The pass runs once per
# minute, so this is a ~2-minute anti-jitter window (a single dropped packet
# or a station mid-restart is not an incident) — the same gate as the
# machine-offline probe (services/heartbeat/liveness.py).
_OFFLINE_AFTER_FAILURES = 2

# One OTLP round-trip budget. The station is a private-network peer; 5s
# covers a slow-but-healthy host while a genuinely offline host still
# refuses fast (connect refused / blackhole), mirroring the status_probe
# budget philosophy (AVA_STATUS_PROBE_TIMEOUT_SECONDS).
_PROBE_TIMEOUT_S = 5.0

_ALERTNAME = "observatory station offline"

# In-process probe state — the watchdog is long-lived, so the consecutive
# failure count and the transition start survive between rounds (the same
# in-process pattern as the page-host cache in gateway/routers/pages.py).
_state: dict[str, Any] = {"failures": 0, "transition_since": None}


def _configured_observability_base() -> str:
    """The validated AVA_OBSERVABILITY_URL base, or "" when unset/malformed.

    The same validation the collector fan-out uses
    (base.telemetry.station_endpoint) — the two consumer paths can never
    disagree about where the station is.
    """
    return validated_observability_base(settings.observability.observability_url)


def resolve_target() -> _StationTarget | None:
    """The station's dial target from the reachability contract.

    The advertised machine_units url when a station unit has registered;
    otherwise the configured AVA_OBSERVABILITY_URL base + OTLP port (with a
    loud warning — the operator configured a remote observatory but no
    station unit has advertised itself). None when no remote observatory is
    configured at all.
    """
    base = _configured_observability_base()
    if not base:
        return None
    try:
        target = resolve_station_target(Database.from_settings(), base)
    except Exception:
        logger.bind(_no_emitter=True, component="station-healthcheck").exception(
            "station probe: cannot read the advertised station address — skipping this round (fail-open)"
        )
        return None
    if target.advertised:
        return target
    logger.bind(_no_emitter=True, component="station-healthcheck").warning(
        "station probe: AVA_OBSERVABILITY_URL is set but no observability-station "
        "unit advertises the OTLP ingress in machine_units (a pure station's "
        "unit_dial_url; a hybrid gateway+station unit advertises its gateway URL "
        "and is not a probe target) — probing the configured base until the "
        "station registers (reachability contract, "
        "conventions/reachability-and-credentials.md)"
    )
    return target


def _station_answers(url: str) -> bool:
    """One bearer-authenticated OTLP round-trip; any 2xx = the ingress serves.

    The bearer is the cluster's telemetry token (the station accepts the same token from its
    capability), read from the private file the gateway home's start publishes: this process
    never holds the human secret it is derived from."""
    from base.cluster.authority.api import read_telemetry_token
    from base.paths import ava_home

    token = read_telemetry_token(ava_home())
    if token is None:
        # A remote observatory with no published telemetry token (an open cluster, or a
        # start that has not run yet) cannot authenticate a probe (and the collector relay
        # already fails closed at converge). Fail open: warn, never block.
        logger.bind(_no_emitter=True, component="station-healthcheck").warning(
            "station probe: no published telemetry token — cannot authenticate the probe of {}; "
            "skipping this round (fail-open)",
            url,
        )
        return True

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(  # noqa: S310 — advertised private-network endpoint, deliberate
        f"{url.rstrip('/')}/v1/traces",
        method="POST",
        data=b'{"resourceSpans":[]}',
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S):  # noqa: S310 — same probe
            return True
    except urllib.error.HTTPError as exc:
        # Any HTTP answer proves the listener is up, but a non-2xx from the
        # OTLP receiver means the ingestion path is not serving (401 auth,
        # 415 body, 5xx) — not alive.
        logger.bind(_no_emitter=True, component="station-healthcheck").warning(
            "station probe: {} answered HTTP {} — ingress not serving",
            url,
            exc.code,
        )
        return False
    except Exception:
        logger.bind(_no_emitter=True, component="station-healthcheck").opt(exception=True).warning(
            "station probe: {} unreachable", url
        )
        return False


def _fire_offline(
    database: Database, target: _StationTarget, state: dict[str, Any], now: datetime
) -> None:
    """Fire (or escalate) the 'observatory station offline' alert; fail-open on any write error."""
    from base.telemetry.alerts import (
        display_language,
        fingerprint,
        notify_im,
        notify_text,
        stamp_notified,
        upsert_alert,
    )

    identity = {"alertname": _ALERTNAME, "station": target.url}
    try:
        with database.connect() as conn:
            severity = transition_severity(
                state["transition_since"],
                now,
                warning_after_s=settings.alerts.transition_warning_seconds,
                error_after_s=settings.alerts.transition_error_seconds,
            )
            if severity is None:
                return
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT starts_at, severity, notified_at FROM alerts "
                    "WHERE labels->>'alertname' = %s AND labels->>'station' = %s "
                    "AND status = 'unresolved' ORDER BY starts_at DESC LIMIT 1",
                    (_ALERTNAME, target.url),
                )
                open_row = cur.fetchone()
            if open_row is not None and open_row[1] == severity and open_row[2] is not None:
                return
            starts_at = open_row[0] if open_row is not None else state["transition_since"]
            alert = {
                "status": "firing",
                "labels": {**identity, "severity": severity},
                "annotations": {
                    "summary": (
                        f"observatory station {target.name or target.url} unreachable for "
                        f"{max(0.0, (now - state['transition_since']).total_seconds()) / 60.0:.1f} "
                        f"minutes ({state['failures']} consecutive failed probes)"
                    )
                },
                "starts_at": starts_at.isoformat(),
                "fingerprint": fingerprint(identity),
            }
            key, _inserted, should_notify, _row = upsert_alert(conn, alert, source="station-probe")
            if should_notify and notify_im(notify_text(alert, display_language(conn))):
                stamp_notified(conn, [key])
    except Exception:
        logger.bind(_no_emitter=True, component="station-healthcheck").exception(
            "station probe: alert write failed (fail-open)"
        )


def _resolve_offline(database: Database, now: datetime) -> None:
    """Resolve every open row for the alertname; fail-open on any write error."""
    from base.telemetry.alerts import (
        display_language,
        notify_im,
        notify_text,
        stamp_notified,
        upsert_alert,
    )

    try:
        with database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT starts_at, fingerprint, severity FROM alerts "
                    "WHERE labels->>'alertname' = %s AND status = 'unresolved' "
                    "ORDER BY starts_at DESC",
                    (_ALERTNAME,),
                )
                open_rows = cur.fetchall()
            if not open_rows:
                return
            lang = display_language(conn)
            for starts_at, fp, severity in open_rows:
                alert = {
                    "status": "resolved",
                    "labels": {"alertname": _ALERTNAME, "severity": severity},
                    "annotations": {"summary": "observatory station reachable again"},
                    "starts_at": starts_at.isoformat(),
                    "ends_at": now.isoformat(),
                    "fingerprint": fp,
                }
                key, _inserted, should_notify, _row = upsert_alert(
                    conn, alert, source="station-probe"
                )
                if should_notify and notify_im(notify_text(alert, lang)):
                    stamp_notified(conn, [key])
    except Exception:
        logger.bind(_no_emitter=True, component="station-healthcheck").exception(
            "station probe: alert resolve write failed (fail-open)"
        )


def _alert_edges(database: Database, target: _StationTarget, *, ok: bool, now: datetime) -> None:
    """Fire/resolve the 'observatory station offline' alert for this target.

    Direct DB write (the gateway watchdog runs with the cluster DB at hand),
    same shape as the machine-offline probe (services/heartbeat/liveness.py):
    fire on the consecutive-failure threshold, escalate WARNING -> ERROR via
    the shared transition clock, resolve on recovery, IM-notify on notify
    edges. Best-effort: alerting must never break the probe.
    """
    state = _state
    if not ok:
        state["failures"] += 1
        if state["transition_since"] is None:
            state["transition_since"] = now
        if state["failures"] < _OFFLINE_AFTER_FAILURES:
            return
        _fire_offline(database, target, state, now)
        return

    # Recovered: reset the episode and resolve every open row for the
    # alertname — the observatory is reachable again regardless of which
    # target address the episode was about.
    recovered = state["failures"] > 0 or state["transition_since"] is not None
    state["failures"] = 0
    state["transition_since"] = None
    if not recovered:
        return
    _resolve_offline(database, now)


def main() -> None:
    init_gateway_process("station")
    target = resolve_target()
    if target is None:
        return  # no remote observatory configured — nothing to probe here
    ok = _station_answers(target.url)
    _alert_edges(Database.from_settings(), target, ok=ok, now=datetime.now(UTC))
    if not ok:
        logger.bind(_no_emitter=True, component="station-healthcheck").warning(
            "station probe: {} did not answer a bearer OTLP probe ({} consecutive failures) — "
            "fail-open: local business is unaffected, alert raised",
            target.url,
            _state["failures"],
        )


if __name__ == "__main__":
    main()
