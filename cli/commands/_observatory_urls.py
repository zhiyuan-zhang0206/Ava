"""Observatory-station URL derivation for the LGTM consumers.

Two-state on AVA_OBSERVABILITY_URL (task #1791, A3+A4): empty (default) keeps
every consumer on its current local loopback endpoint; non-empty switches the
Grafana Loki/Prometheus datasources, the alert webhook target, and the
otel-collector gateway fan-out to the remote observatory station (the PG
datasource stays on the cluster's data plane — it never follows the
observatory, #3606). Split out of _lgtm_native.py so the URL contract has
one home shared by the native renderer and the collector.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from shared.config import settings


def _observability_datasource_urls() -> tuple[str, str, str]:
    """The (loki, prometheus, pg) datasource URLs for the rendered Grafana tree.

    Two-state on AVA_OBSERVABILITY_URL: empty (default) keeps the current
    loopback endpoints (from the per-service settings, which default to this
    host's native backends); non-empty points Loki/Prometheus at the remote
    observatory station (base URL + the service's own port). PG keeps its
    scheme-less host:port form but NEVER follows the observatory — it is the
    CLUSTER's own database (#3606) and stays on the data plane (its host
    derives from the runner's db_url; task #1752 moves PG on its own track).
    """

    obs = settings.observability
    base = _validated_observability_base(obs.observability_url)
    if base:
        return f"{base}:3100", f"{base}:9090", _pg_datasource_host_port()
    return (
        obs.telemetry_loki_url.rstrip("/"),
        obs.telemetry_prometheus_url.rstrip("/"),
        "127.0.0.1:5433",
    )


def _pg_datasource_host_port() -> str:
    """The cluster's own PG host:port for the rendered Grafana SQL datasource.

    PG externalization (#1752) is an independent track from the observatory
    (stage C moves the observatory while PG stays on the gateway), so the
    datasource host must derive from the data plane, not from
    AVA_OBSERVABILITY_URL. The runner's db_url is the single source of truth:
    under a remote observatory it names the cluster's data-plane host, and
    ``url_host`` is the same A5 single point the admin/status dials use. A
    db_url that still names loopback under a remote observatory renders a
    loopback datasource (only correct when Grafana runs on the PG host
    itself) — warn loudly, same pattern as the Tempo topology warning.
    """
    from shared.url_secret import url_host

    host = url_host(settings.data_plane.db_url)
    if host in ("127.0.0.1", "localhost", "::1"):
        print(
            "lgtm native: AVA_OBSERVABILITY_URL is set but the data-plane db_url "
            f"still names {host} — the rendered PG datasource will dial the "
            "Grafana host's own loopback; point db_url at the cluster's "
            "data-plane host (task #1752) for a remote observatory.",
            file=sys.stderr,
        )
    return f"{host}:5433"


def _validated_observability_base(observability_url: str) -> str:
    """Return the observatory base URL when well-formed, else "" after a warning.

    The setting's contract is ``scheme://host`` with no port and no path (each
    consumer appends its own port). A malformed value would silently render
    broken datasource URLs on every converge, so validate once and warn — the
    same pattern as the Tempo topology warning in _render_configs. A malformed
    value falls back to local loopback (the safe default) instead of rendering
    garbage URLs.
    """
    base = observability_url.strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base)
    problems: list[str] = []
    if parsed.scheme not in ("http", "https"):
        problems.append(f"scheme must be http/https (got {parsed.scheme!r})")
    if not parsed.hostname:
        problems.append("missing host")
    if parsed.port is not None:
        problems.append("port must be omitted (consumers append their own)")
    if parsed.path not in ("", "/"):
        problems.append(f"path must be omitted (got {parsed.path!r})")
    if problems:
        print(
            "lgtm native: AVA_OBSERVABILITY_URL "
            f"{observability_url!r} is malformed ({'; '.join(problems)}) — "
            "falling back to local loopback endpoints",
            file=sys.stderr,
        )
        return ""
    return base


def _alerts_webhook_url() -> str:
    """The Grafana alert webhook target — the GATEWAY's own reachable address.

    Deliberately NOT derived from observability_url: the alert ingest endpoint
    lives on this cluster's gateway, wherever the observatory is. Two-state:
    empty observability_url (local observatory) keeps the byte-identical
    loopback 127.0.0.1:8000 default; a remote observatory needs the gateway's
    reachable host (shared.machine.reachable_host) so the remote Grafana can
    dial the ingest endpoint. Self-dialing a tailnet IP from the gateway host
    itself can hit VPN hairpin filtering (pgbouncer probe incident), which is
    exactly why the loopback form is kept when no remote observatory is set.
    """
    from shared.machine import reachable_host

    if settings.observability.observability_url:
        host = reachable_host()
        print(
            "lgtm native: alert webhook will point at the gateway's reachable "
            f"address http://{host}:8000 — this presumes a REMOTE observatory "
            "Grafana consumes the rendered contact.yml (delivery lands with the "
            "observatory deployment, stage B). With a local Grafana consumer, "
            "self-dialing a tailnet address can hit VPN hairpin filtering; keep "
            "AVA_OBSERVABILITY_URL empty until the remote mechanism exists.",
            file=sys.stderr,
        )
        return f"http://{host}:8000/api/alerts"
    return "http://127.0.0.1:8000/api/alerts"


def _atomic_write(path: Path, content: str) -> None:
    """Replace ``path`` with ``content`` atomically (mkstemp + rename).

    Grafana's provisioning watcher may read a file mid-write; a torn render
    would provision a half file. The temp file is written fully before the
    rename publishes it.
    """
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
