"""Observatory-station URL derivation for the LGTM consumers.

Two-state on AVA_OBSERVABILITY_URL (task #1791, A3+A4): empty (default) keeps
every consumer on its current local loopback endpoint; non-empty switches the
Grafana datasources, the alert webhook target, and the otel-collector gateway
fan-out to the remote observatory station. Split out of _lgtm_native.py so
the URL contract has one home shared by the native renderer and the collector.
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
    host's native backends); non-empty uses the observatory station's base URL
    with each service's port appended. PG keeps its scheme-less host:port
    form — the observatory host must serve this cluster's ava_main on :5433
    with the grafana_ro role or the SQL panels break (#3606).
    """

    obs = settings.observability
    base = _validated_observability_base(obs.observability_url)
    if base:
        pg_host = (
            urlparse(base).hostname
            or base.removeprefix("http://").removeprefix("https://").split(":")[0]
        )
        return f"{base}:3100", f"{base}:9090", f"{pg_host}:5433"
    return (
        obs.telemetry_loki_url.rstrip("/"),
        obs.telemetry_prometheus_url.rstrip("/"),
        "127.0.0.1:5433",
    )


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
