"""Remote observatory-station probe — run every 60s by the GATEWAY watchdog.

Lives in services/wake/heartbeat/ (not services/supervision/healthchecks/) on purpose: it is gateway-only —
the runner watchdog imports the healthcheck roster too, and a module under
services/supervision/healthchecks/ would drag the gateway-side station resolution into the runner
profile (test_gateway_consumer_guard). The watchdog resolves it by dotted string, so the
runner closure never contains it.

The GATEWAY's probe of a REMOTE observatory station (WP4, task #1946;
docs/conventions/data/reachability-and-credentials.md). When `AVA_OBSERVABILITY_URL`
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
queue and the local stack is untouched. The probe only logs a
warning (no alert: the station hosts the observability backends, so a station
that cannot ingest cannot carry its own failure signal). Every failure path is
caught and logged; main() never raises.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

from base.config import settings
from base.db import Database
from base.log import init_gateway_process, logger
from base.telemetry.station_endpoint import StationTarget as _StationTarget
from base.telemetry.station_endpoint import resolve_station_target, validated_observability_base

_log = logging.getLogger("services.wake.heartbeat.station_probe")

# One OTLP round-trip budget. The station is a private-network peer; 5s
# covers a slow-but-healthy host while a genuinely offline host still
# refuses fast (connect refused / blackhole), mirroring the status_probe
# budget philosophy (AVA_STATUS_PROBE_TIMEOUT_SECONDS).
_PROBE_TIMEOUT_S = 5.0


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
        "docs/conventions/data/reachability-and-credentials.md)"
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


def main() -> None:
    init_gateway_process("station")
    target = resolve_target()
    if target is None:
        return  # no remote observatory configured — nothing to probe here
    ok = _station_answers(target.url)
    if not ok:
        logger.bind(_no_emitter=True, component="station-healthcheck").warning(
            "station probe: {} did not answer a bearer OTLP probe — "
            "fail-open: local business is unaffected",
            target.url,
        )


if __name__ == "__main__":
    main()
