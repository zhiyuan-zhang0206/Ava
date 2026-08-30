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
unit advertises in `machine_units` (`shared.machines.unit_dial_url`), not a
bare connect — and authenticates with the cluster bearer, exactly like the
collector relay that ships telemetry to it.

The probe is an OTLP round-trip: `POST <advertised url>/v1/traces` with an
empty `ExportTraceServiceRequest` and `Authorization: Bearer <secret>`. Any
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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import psycopg

import shared.db
from shared.config import settings
from shared.log import init_gateway_process, logger
from shared.transition import transition_severity

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


@dataclass(frozen=True)
class _StationTarget:
    """The station's dial target: the advertised URL or the configured base."""

    url: str
    advertised: bool
    name: str | None = None


def _configured_observability_base() -> str:
    """The validated AVA_OBSERVABILITY_URL base, or "" when unset/malformed.

    The same validation the collector fan-out uses
    (cli/commands/_observatory_urls.py) — the two consumer paths can never
    disagree about where the station is.
    """
    from cli.commands._observatory_urls import _validated_observability_base

    return _validated_observability_base(settings.observability.observability_url)


def _advertised_station_unit(conn: psycopg.Connection) -> tuple[str, str] | None:
    """The (machine_name, url) of a live observability-station unit that
    advertises the OTLP ingress, or None.

    The reachability contract: the address the station itself advertised at
    registration (`shared.machines.unit_dial_url`). Read from machine_units
    directly (not the composed machines row) because a co-located gateway
    unit's URL wins the composed `machines.gateway_url` — the station's own
    advertised address is what the probe must dial.

    Only a unit whose advertised url carries the OTLP ingress port
    (`AVA_TELEMETRY_OTLP_PORT`, single source) qualifies: a pure station
    advertises exactly that, while a hybrid gateway+station unit advertises
    its gateway URL (unit_dial_url lets the gateway capability win) — probing
    that address would hit the gateway API and alert forever (QA #1156
    NIT-2). No qualifying unit → None, and the caller falls back to the
    configured observability base with a warning.
    """
    ingress_port = settings.observability.telemetry_otlp_port
    with conn.cursor() as cur:
        cur.execute(
            "SELECT machine_name, url FROM machine_units "
            "WHERE serve_observability_station AND stopped_at IS NULL "
            "AND url IS NOT NULL ORDER BY machine_name, home"
        )
        for name, url in cur.fetchall():
            if urlparse(str(url)).port == ingress_port:
                return str(name), str(url)
    return None


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
        with shared.db.connect() as conn:
            advertised = _advertised_station_unit(conn)
    except Exception:
        logger.bind(_no_emitter=True, component="station-healthcheck").exception(
            "station probe: cannot read the advertised station address — skipping this round (fail-open)"
        )
        return None
    if advertised is not None:
        name, url = advertised
        return _StationTarget(url=url, advertised=True, name=name)
    logger.bind(_no_emitter=True, component="station-healthcheck").warning(
        "station probe: AVA_OBSERVABILITY_URL is set but no observability-station "
        "unit advertises the OTLP ingress in machine_units (a pure station's "
        "unit_dial_url; a hybrid gateway+station unit advertises its gateway URL "
        "and is not a probe target) — probing the configured base until the "
        "station registers (reachability contract, "
        "conventions/reachability-and-credentials.md)"
    )
    return _StationTarget(
        url=f"{base}:{settings.observability.telemetry_otlp_port}", advertised=False
    )


def _station_answers(url: str) -> bool:
    """One bearer-authenticated OTLP round-trip; any 2xx = the ingress serves."""
    secret = settings.data_plane.cluster_secret
    if not secret:
        # A remote observatory without a cluster secret cannot authenticate a
        # probe (and the collector relay already fails closed at converge).
        # Fail open: warn, never block.
        logger.bind(_no_emitter=True, component="station-healthcheck").warning(
            "station probe: no AVA_CLUSTER_SECRET set — cannot authenticate the probe of {}; "
            "skipping this round (fail-open)",
            url,
        )
        return True
    headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
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
        logger.bind(_no_emitter=True, component="station-healthcheck").warning(
            "station probe: {} unreachable", url, exc_info=True
        )
        return False


def _alert_edges(target: _StationTarget, *, ok: bool, now: datetime) -> None:
    """Fire/resolve the 'observatory station offline' alert for this target.

    Direct DB write (the gateway watchdog runs with the cluster DB at hand),
    same shape as the machine-offline probe (services/heartbeat/liveness.py):
    fire on the consecutive-failure threshold, escalate WARNING -> ERROR via
    the shared transition clock, resolve on recovery, IM-notify on notify
    edges. Best-effort: alerting must never break the probe.
    """
    from shared.alerts import (
        display_language,
        fingerprint,
        notify_im,
        notify_text,
        stamp_notified,
        upsert_alert,
    )

    state = _state
    if not ok:
        state["failures"] += 1
        if state["transition_since"] is None:
            state["transition_since"] = now
        if state["failures"] < _OFFLINE_AFTER_FAILURES:
            return
        identity = {"alertname": _ALERTNAME, "station": target.url}
        try:
            with shared.db.connect() as conn:
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
                key, _inserted, should_notify, _row = upsert_alert(
                    conn, alert, source="station-probe"
                )
                if should_notify and notify_im(notify_text(alert, display_language(conn))):
                    stamp_notified(conn, [key])
        except Exception:
            logger.bind(_no_emitter=True, component="station-healthcheck").exception(
                "station probe: alert write failed (fail-open)"
            )
        return

    # Recovered: reset the episode and resolve every open row for the
    # alertname — the observatory is reachable again regardless of which
    # target address the episode was about.
    recovered = state["failures"] > 0 or state["transition_since"] is not None
    state["failures"] = 0
    state["transition_since"] = None
    if not recovered:
        return
    try:
        with shared.db.connect() as conn:
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


def main() -> None:
    init_gateway_process("station")
    target = resolve_target()
    if target is None:
        return  # no remote observatory configured — nothing to probe here
    ok = _station_answers(target.url)
    _alert_edges(target, ok=ok, now=datetime.now(UTC))
    if not ok:
        logger.bind(_no_emitter=True, component="station-healthcheck").warning(
            "station probe: {} did not answer a bearer OTLP probe ({} consecutive failures) — "
            "fail-open: local business is unaffected, alert raised",
            target.url,
            _state["failures"],
        )


if __name__ == "__main__":
    main()
