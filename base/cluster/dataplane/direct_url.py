"""The admin-plane Postgres URL: this cluster's access URL never routed through PgBouncer.

`base.db` dials a direct (unpooled) connection from a resolver its composition root
injects (`Database(..., direct_url=ClusterDirectUrl(machine.host))`), because the
resolution reads this home's cluster record, which sits above the database layer.
"""

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from base.cluster.record import get_record
from base.db.config import DbConfig, db_config_from_settings
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
from base.host.net.predicates import is_loopback_host
from base.host.net.url_secret import url_with_port
from base.log import logger


@dataclass(frozen=True)
class ClusterDirectUrl:
    """The resolver a composition root hands to `Database`: `direct_db_url` bound to its host."""

    local_host: Callable[[], str]

    def __call__(self, config: DbConfig) -> str:
        return direct_db_url(config, local_host=self.local_host)


def direct_db_url(
    config: DbConfig | None = None, *, local_host: Callable[[], str] | None = None
) -> str:
    """The admin-plane Postgres URL: this cluster's `AVA_DB_URL` never routed
    through PgBouncer.

    `AVA_DB_URL` carries the pooler listener port whenever pooling is enabled
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
    with a WARNING:
    the "direct" dial silently routes through the gateway's pooler (the
    migration authority model keeps a runner from mutating the schema in
    practice, but the exemption itself is unavailable and must not be silent).

    The placeholder URL passes through byte-identical (the connect guard
    matches it byte-for-byte), and a home with no record (no gateway capability)
    keeps `AVA_DB_URL` as-is rather than guessing.
    """
    cfg = config or db_config_from_settings()
    url = cfg.db_url
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
    if cfg.pgbouncer_enabled and (local or host == _gateway_url_host()):
        _log_pooled_fallback(host, port)
    return url


def _gateway_url_host() -> str:
    """The host this process dials as its gateway (`AVA_GATEWAY_URL`), lowercased and
    unbracketed; "" when the gateway domain is not constructed in this process's profile (or
    unset). It tells a split agent-runner's URL, which names the GATEWAY's pooler (a foreign
    host whose direct-exemption warning is meaningful), apart from a remote/SaaS plane URL,
    which is direct by definition and must dial silently (Task #1752).

    Imported at call time so importing this module (every composition root does) does not
    pull the eager config chain; resolving a direct dial has loaded it by then anyway."""
    from base.config import settings

    try:
        url = settings.gateway.gateway_url
    except AttributeError:
        return ""
    return (urlsplit(url).hostname or "").lower().removeprefix("[").removesuffix("]")


def _from_record(url: str, port: int) -> str | None:
    """The direct URL this home's record dictates for `url`, or None when it names neither port."""
    from base.paths import ava_home

    rec = get_record(ava_home())
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
    # pooler): every `ava start` logs one, so it is INFO, not a WARNING
    # (2026-10-03 triage).
    logger.info(
        "direct_db_url: AVA_DB_URL names {host}:{port}, which this home's record "
        "does not name as its PgBouncer or Postgres port (a split agent-runner's "
        "URL names the gateway's pooler, resolvable only on the gateway box). "
        "Returning AVA_DB_URL as-is — the admin-plane dial routes through "
        "PgBouncer; the migration authority model keeps a non-gateway host "
        "from mutating the schema, but the direct exemption is unavailable here.",
        host=host,
        port=port,
    )
