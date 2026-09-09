"""Remote station ingress shared by collector rendering and station probes."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import psycopg

from shared.config import settings


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


def resolve_station_target(base: str) -> StationTarget:
    """Use the station's own port, never the consuming unit's receiver port.

    Database discovery errors propagate: rendering an invented target during
    an outage would persist it beyond recovery. Probe callers may skip a round.
    """
    import shared.db

    with shared.db.connect() as conn:
        advertised = advertised_station_unit(conn, base)
    if advertised is not None:
        name, url = advertised
        return StationTarget(url=url, advertised=True, name=name)
    return StationTarget(
        url=f"{base}:{settings.observability.observability_otlp_port}", advertised=False
    )
