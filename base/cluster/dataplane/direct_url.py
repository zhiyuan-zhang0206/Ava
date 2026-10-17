from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from base.cluster.record import ClusterRecord, get_record
from base.db.config import DbConfig
from base.db.connections import DirectResolution
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
from base.host.net.predicates import is_loopback_host
from base.host.net.url_secret import url_with_port


@dataclass(frozen=True)
class ClusterDirectUrl:
    local_host: Callable[[], str]

    def __call__(self, config: DbConfig) -> DirectResolution:
        return resolve_direct_url(config, local_host=self.local_host)


def resolve_direct_url(
    config: DbConfig, *, local_host: Callable[[], str] | None = None
) -> DirectResolution:
    url = config.db_url
    if url == PLACEHOLDER_DB_URL:
        return DirectResolution(url=url, is_direct=True)
    try:
        parts = urlsplit(url)
        port = parts.port
        host = (parts.hostname or "").lower()
    except ValueError:
        return DirectResolution(url=url, is_direct=True)
    if port is None:
        return DirectResolution(url=url, is_direct=True)
    if not config.pgbouncer_enabled:
        return DirectResolution(url=url, is_direct=True)

    authority = f"{host}:{port}"
    if not _is_local(host, local_host):
        return DirectResolution(
            url=url,
            is_direct=False,
            reason=(
                f"the database URL names {authority}, which is another machine — that machine's "
                "cluster record is not readable from here, so nothing here says whether that "
                "port is a pooler. Dial the direct port from the machine that owns the cluster, "
                "or point the URL at this machine's own PgBouncer or Postgres port."
            ),
        )
    record = _record()
    if record is None:
        return DirectResolution(
            url=url,
            is_direct=False,
            reason=(
                f"the database URL names {authority}, but this home has no cluster record (it "
                "owns no data plane), so nothing here says whether that port is a pooler. Run "
                "this step on the machine that owns the cluster."
            ),
        )
    postgres_port = record.ports["postgres"]
    if port == postgres_port:
        return DirectResolution(url=url, is_direct=True)
    pooler_port = record.ports["pgbouncer"]
    if port == pooler_port:
        return DirectResolution(url=url_with_port(url, postgres_port), is_direct=True)
    return DirectResolution(
        url=url,
        is_direct=False,
        reason=(
            f"the database URL names {authority}, but this home's cluster record names PgBouncer "
            f"port {pooler_port} and Postgres port {postgres_port} — the URL names neither, so "
            "this home cannot tell what that port is. Point the URL at one of the two, or "
            "correct the record."
        ),
    )


def _is_local(host: str, local_host: Callable[[], str] | None) -> bool:
    if is_loopback_host(host):
        return True
    if local_host is None:
        raise RuntimeError("direct database host resolution requires an explicit local_host reader")
    return host == local_host().lower()


def _record() -> ClusterRecord | None:
    from base.paths import meiva_home

    return get_record(meiva_home())
