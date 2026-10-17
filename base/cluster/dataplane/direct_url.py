"""The admin-plane Postgres URL: this cluster's access URL never routed through PgBouncer.

`base.db` dials a direct (unpooled) connection from a `ClusterDirectUrl` resolver its
composition root injects with explicit host and gateway URL readers. Resolution reads
this home's cluster record, which sits above the database layer.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from base.cluster.record import get_record
from base.db.config import DbConfig
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
from base.host.net.predicates import is_loopback_host
from base.host.net.url_secret import url_with_port
from base.log import logger


@dataclass(frozen=True)
class ClusterDirectUrl:
    """The resolver a composition root binds to its lazy host and gateway URL readers."""

    local_host: Callable[[], str]
    gateway_url_reader: Callable[[], str] = field(kw_only=True, repr=False)

    def __call__(self, config: DbConfig) -> str:
        return direct_db_url(
            config, gateway_url_reader=self.gateway_url_reader, local_host=self.local_host
        )


def direct_db_url(
    config: DbConfig,
    *,
    gateway_url_reader: Callable[[], str],
    local_host: Callable[[], str] | None = None,
) -> str:
    """The admin-plane Postgres URL: this cluster's `MEIVA_DB_URL` never routed
    through PgBouncer.

    `MEIVA_DB_URL` carries the pooler listener port whenever pooling is enabled
    (the one-URL design: a normal process dials it as-is and never knows the
    pooler exists), so the admin plane — migrations (SESSION advisory locks),
    pg_dump (needs a real backend session), provisioning, auth probes — must
    derive the direct Postgres URL instead. The derivation swaps ONLY the port,
    from the pooler listener to the cluster's direct Postgres port, both read
    from this home's own cluster record: the pooler is co-located with its
    Postgres on the gateway box, so host and credentials are identical. When the
    URL does not name the pooler (pooling off, an operator stand-in URL) it is
    returned verbatim — already direct.

    The record lookup matches the URL's host:port against this home's record
    only, and only when the URL names this box. A home knows no other cluster, so
    a unit whose URL names another home's pooler (a split agent-runner on this or
    another box) cannot be resolved here. In that case the URL is returned as-is
    with an INFO line:
    the "direct" dial silently routes through the gateway's pooler (the
    migration authority model keeps a runner from mutating the schema in
    practice, but the exemption itself is unavailable and must not be silent).

    The placeholder URL passes through byte-identical (the connect guard
    matches it byte-for-byte), and a home with no record (no gateway capability)
    keeps `MEIVA_DB_URL` as-is rather than guessing.
    """
    url = config.db_url
    if url == PLACEHOLDER_DB_URL:
        return url
    try:
        parts = urlsplit(url)
        port = parts.port
        host = (parts.hostname or "").lower()
    except ValueError:
        return url
    if port is None:
        return url

    def matches_local_host() -> bool:
        if local_host is None:
            raise RuntimeError(
                "direct database host resolution requires an explicit local_host reader"
            )
        return host == local_host().lower()

    # Only a loopback or self-named host can be resolved against this home's
    # record — a remote host's record lives on that box, not here.
    local = is_loopback_host(host) or matches_local_host()
    direct = _from_record(url, port) if local else None
    if direct is not None:
        return direct
    # This home's record does not explain the URL's port: a local operator
    # stand-in, or a split runner naming the gateway's pooler. A remote/SaaS plane (Task #1752)
    # is direct by definition — no local pooler exists — so it dials silently.
    if config.pgbouncer_enabled and (local or host == _gateway_url_host(gateway_url_reader())):
        _log_pooled_fallback(host, port)
    return url


def _gateway_url_host(gateway_url: str) -> str:
    """The host this process dials as its gateway (`MEIVA_GATEWAY_URL`), lowercased and
    unbracketed; "" when its explicit URL is empty. It tells a split agent-runner's URL,
    which names the gateway's pooler (a foreign host whose fallback log is meaningful),
    apart from a remote/SaaS plane URL,
    which is direct by definition and must dial silently (Task #1752)."""
    return (urlsplit(gateway_url).hostname or "").lower().removeprefix("[").removesuffix("]")


def _from_record(url: str, port: int) -> str | None:
    """The direct URL this home's record dictates for `url`, or None when it names neither port."""
    from base.paths import meiva_home

    rec = get_record(meiva_home())
    if rec is None:
        return None
    if port == rec.ports["pgbouncer"]:
        # URL names this cluster's pooler -> swap to its direct pg port.
        return url_with_port(url, rec.ports["postgres"])
    if port == rec.ports["postgres"]:
        # URL already names Postgres (pooling off / a stand-in on a cluster
        # port) -> already direct.
        return url
    return None


def _log_pooled_fallback(host: str, port: int) -> None:
    # Expected shape on a split agent-runner (its URL names the gateway's
    # pooler): every `meiva start` logs one, so it is INFO, not a WARNING
    # (2026-10-03 triage).
    logger.info(
        "direct_db_url: MEIVA_DB_URL names {host}:{port}, which this home's record "
        "does not name as its PgBouncer or Postgres port (a split agent-runner's "
        "URL names the gateway's pooler, resolvable only on the gateway box). "
        "Returning MEIVA_DB_URL as-is — the admin-plane dial routes through "
        "PgBouncer; the migration authority model keeps a non-gateway host "
        "from mutating the schema, but the direct exemption is unavailable here.",
        host=host,
        port=port,
    )
