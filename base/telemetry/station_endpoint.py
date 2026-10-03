"""Remote station ingress shared by collector rendering and station probes."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from urllib.parse import urlparse, urlsplit

import psycopg

from base.config import settings
from base.db import Database


@dataclass(frozen=True)
class StationTarget:
    url: str
    advertised: bool
    name: str | None = None


def advertised_station_unit(conn: psycopg.Connection, base: str) -> tuple[str, str] | None:
    """Find this host's pure station; hybrid advertisements are gateway/ops URLs."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT machine_name, url FROM machine_units "
            "WHERE serve_observability_station AND NOT serve_gateway "
            "AND NOT serve_agent_runner AND stopped_at IS NULL "
            "AND url IS NOT NULL ORDER BY machine_name, home"
        )
        matches = [
            (str(name), str(url).rstrip("/"))
            for name, url in cur.fetchall()
            if urlsplit(str(url)).hostname == urlsplit(base).hostname
        ]
    if len({url for _, url in matches}) > 1:
        raise RuntimeError(f"multiple station ingress advertisements for {base}")
    return matches[0] if matches else None


def resolve_station_target(db: Database, base: str) -> StationTarget:
    """Use the station's own port, never the consuming unit's receiver port.

    Database discovery errors propagate: rendering an invented target during
    an outage would persist it beyond recovery. Probe callers may skip a round.
    """
    with db.connect() as conn:
        advertised = advertised_station_unit(conn, base)
    if advertised is not None:
        name, url = advertised
        return StationTarget(url=url, advertised=True, name=name)
    return StationTarget(
        url=f"{base}:{settings.observability.observability_otlp_port}", advertised=False
    )


def validated_observability_base(observability_url: str) -> str:
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
        print(  # noqa: T201 - operator-facing warning on stderr, as the cli render path always printed it
            "lgtm native: AVA_OBSERVABILITY_URL "
            f"{observability_url!r} is malformed ({'; '.join(problems)}) — "
            "falling back to local loopback endpoints",
            file=sys.stderr,
        )
        return ""
    return base
