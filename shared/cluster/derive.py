"""Derived cluster facts — identity, labels, session names, channels, env.

Everything a cluster derives rather than stores: the data-plane identity read
from `.env` URLs as data (`identity_from_url` / `db_identity` /
`redis_identity`, plus the fixed `DATA_PLANE_IDENTITY` at birth); the
home-derived display label and OS-artifact slug (`home_label` / `home_slug` /
`slug_for_home`); the session-name composer (`session_name`); the redis
channel/wake-key names and admin URL; and
`derive_env` — the record-to-env mapping a unit needs (plus its base-URL
helper `per_cluster_base_urls`).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from shared import cluster
from shared.config import settings
from shared.env_registry import health_port_env_aliases
from shared.url_secret import url_with_port, url_with_userinfo

# The db / Postgres-role / redis-ACL identifier a newly-born cluster uses. Fixed:
# every cluster owns its instance (exactly one tenant), so the identifier carries
# no cluster distinction. Existing clusters keep whatever identifier their `.env`
# URLs carry (names-as-data) until an ops rename.
DATA_PLANE_IDENTITY = "ava"

# The runner role's FIXED name and the gateway-.env key carrying its password.
# Unlike the main data-plane identity (names-as-data, read back from URL
# userinfo), the runner role is deliberately fixed: every runner in the cluster
# connects as `ava_runner`, and the role name is part of the bootstrap
# projection contract (GET /api/bootstrap?role=runner), not a per-cluster fact.
# The password is a gateway-side secret — generated at install / by
# `ava cluster ensure-runner-role`, written to the gateway's .env as
# AVA_RUNNER_DB_PASSWORD, and only ever distributed inside the projected
# AVA_DB_URL (never as a standalone bootstrap field).
RUNNER_ROLE = "ava_runner"
RUNNER_DB_PASSWORD_ENV = "AVA_RUNNER_DB_PASSWORD"  # noqa: S105 — the env KEY name, not a credential


def default_home() -> Path:
    """The default (prod) cluster home, `~/.ava` — the one home whose install
    uses the fixed legacy port block and whose checkout anchors without a
    pointer."""
    return Path.home() / ".ava"


def is_default_home(home: Path) -> bool:
    """Whether `home` is the default prod home (`~/.ava`)."""
    return Path(home).expanduser().resolve() == default_home().resolve()


def home_label(home: Path) -> str:
    """The human-facing label for a cluster — its home's basename, computed on
    the fly (pure display; renaming the directory changes only the label)."""
    return Path(home).expanduser().name


def home_slug(home: Path) -> str:
    """A filesystem/launchd-safe slug for a cluster home: `<basename>-<8-hex>`
    (leading dots stripped; the hash disambiguates two homes sharing a
    basename). Used for OS-level artifacts that need a per-cluster token outside
    the home itself — launchd/cron labels, the short pg socket dir under /tmp."""
    base = Path(home).expanduser().name.lstrip(".") or "home"
    digest = hashlib.sha1(str(Path(home).expanduser()).encode(), usedforsecurity=False).hexdigest()[
        :8
    ]
    return f"{base}-{digest}"


def slug_for_home(home: Path | None) -> str:
    """`home_slug` of `home`, or of this process's own home when `home` is None.

    The single place the "which cluster" choice is resolved for OS-level job
    names: callers that act on another cluster pass its home path, and everything
    below receives a plain slug so it cannot re-derive one from process state.
    """
    from shared.paths import ava_home

    return home_slug(home if home is not None else ava_home())


def identity_from_url(url: str) -> str:
    """The data-plane identity (db / role / ACL user) a connection URL carries —
    its username, read as DATA. Every idempotent ensure path (role password
    re-affirm, redis ACL re-affirm, pgbouncer identity) must take its identity
    from here, never re-derive it, so a cluster keeps working across an ops
    rename window.

    Raises:
        ValueError: the URL carries no username. The identity is READ, never
            guessed — a wiped/malformed `.env` URL must fail loudly here, not
            send the ensure machinery off re-affirming a wrong role/ACL user
            (which turns every downstream error into an unexplained auth
            failure). Fix the URL (e.g. `postgresql://ava:...@host:port/ava`).
    """
    user = urlsplit(url).username
    if not user:
        raise ValueError(
            f"data-plane URL carries no username (the db/role/ACL identity): {url!r}. "
            "Identity is read from the URL as data, never guessed — fix the .env URL "
            "(e.g. postgresql://ava:<secret>@host:port/ava, redis://ava:<secret>@host:port/0)."
        )
    return user


def db_identity() -> str:
    """This cluster's Postgres db/role identity, read from its own db_url.

    Raises:
        ValueError: db_url carries no username (see identity_from_url)."""
    return identity_from_url(settings.data_plane.db_url)


def redis_identity() -> str:
    """This cluster's redis ACL user, read from its own redis_url.

    Raises:
        ValueError: redis_url carries no username (see identity_from_url)."""
    return identity_from_url(settings.data_plane.redis_url)


def session_name(service: str) -> str:
    """Compose the session name `ava-<service>`.

    Single source for cli / gateway / services (all import this from shared, the
    lowest layer). The `ava-` prefix marks the session as belonging to this
    system; neither machine nor cluster is encoded — the session backend is
    host-local AND per-home, so the home already scopes every session.
    """
    return f"ava-{service}"


def fe_build_env() -> str:
    """The `NEXT_PUBLIC_*` assignment prefix the frontend's `npm run build` needs.

    NEXT_PUBLIC_* are inlined into the JS bundle at build time, not read at
    runtime, and the session env only forwards AVA_* + bootstrap secrets — so a
    NEXT_PUBLIC_GATEWAY_PORT placed in a unit's `.env` never reaches the build
    subprocess. Instead derive it from the single source of truth
    (AVA_GATEWAY_PORT == settings.gateway.gateway_port) and inject it directly on the
    build command line. The browser then dials `${hostname}:${gateway_port}`
    (frontend/src/lib/api.ts), correct on any host whose gateway is not on the
    default 8000 (e.g. the prod VPS on 8800, co-located with another service
    holding 8000).

    Single source for both build paths — the canonical ServiceSpec
    (cli/commands/_repo.py) AND the frontend healthcheck respawn
    (services/healthchecks/frontend.py) — so a watchdog restart can never bake a
    different (stale) gateway port than `ava start` did.
    """
    return f"NEXT_PUBLIC_GATEWAY_PORT={settings.gateway.gateway_port}"


def redis_admin_url() -> str:
    """libpq-style redis URL connecting as the `default` (admin) user, used to
    provision the cluster's redis ACL user + run admin-plane probes. Not a
    per-cluster runtime identity.

    Every cluster owns its redis instance, whose `requirepass` IS the cluster
    secret (single-tenant, so the `default` admin password equals it — no separate
    box-level admin secret). Host/port come from this cluster's own `redis_url`
    (loopback + its per-cluster port), never a hardcoded 6379."""
    parts = urlsplit(settings.data_plane.redis_url)
    # `parts.port` is None when the URL carries no explicit port — the
    # stringified ":None" would be a connect error with a confusing message
    # (audit 2026-08-08 P3: settings normally carry the port, this is the
    # defensive floor).
    port = parts.port or 6379
    return f"redis://default:{settings.data_plane.cluster_secret}@{parts.hostname or '127.0.0.1'}:{port}"


def redis_channel_prefix() -> str:
    """The pub/sub channel prefix (`ava`) — the events channel is `<prefix>:events`,
    so strip that suffix. Fixed across clusters now that each owns its redis: there
    is no neighbour to prefix away from."""
    return settings.data_plane.events_channel.removesuffix(":events")


def inbound_channel(agent_id: int) -> str:
    """The Redis pub/sub channel an agent waits on for inbound wake-ups —
    `ava:inbound:<agent_id>`, via `redis_channel_prefix()`.

    Publish (`shared.db.insert_inbound_message` / `insert_compact_request_inbound`,
    `ava.self`) and subscribe (`RedisInboundListener`) both derive the channel here
    so the two halves can never drift."""
    return f"{redis_channel_prefix()}:inbound:{agent_id}"


# TTL for the wake-key breadcrumb — must exceed the claim wait budget (30s) + a reconnect.
WAKE_KEY_TTL_S = 60


def wake_key(agent_id: int) -> str:
    """Redis key SETEXed alongside every inbound pub/sub wake —
    `ava:wake:<agent_id>`. Pub/sub is fire-and-forget: a wake published while
    the listener is down is otherwise lost until the 30s SELECT recheck. The
    listener GETDELs this breadcrumb after (re)subscribing, so a lost wake
    triggers the SELECT recheck immediately. Keys sit in the cluster ACL
    user's `~*` grant — no ACL change needed."""
    return f"{redis_channel_prefix()}:wake:{agent_id}"


def ensure_cluster_redis_acl(
    user: str, *, redis_admin_url: str, cluster_secret: str, channel_prefix: str
) -> None:
    """Create (or re-affirm) the cluster's redis ACL user `user` — the runtime
    redis identity, mirroring the per-cluster Postgres role. Idempotent; safe on
    every bring-up. `user` is names-as-data: read from the cluster's own
    redis_url (`identity_from_url`) for an existing cluster, `DATA_PLANE_IDENTITY`
    at birth. The user authenticates with the cluster secret and is scoped
    to keys (`~*`) + pub/sub channels (`&<channel_prefix>:*`); `-@dangerous` denies
    FLUSHALL / CONFIG / SHUTDOWN. The secret travels over the redis connection, never
    a process argv.

    `resetpass` precedes `>cluster_secret`: Redis ACL passwords are additive by
    default (`>password` ADDS a valid password rather than replacing the set), so
    without it a secret rotation would leave the PREVIOUS secret still
    authenticating this user indefinitely — confirmed empirically while building
    `scripts/rotate_cluster_secret.py`. `resetpass` clears the password list first,
    so re-affirming with an unchanged secret still ends at exactly one valid
    password (this call is idempotent either way), and re-affirming with a
    rotated one actually invalidates the old one.

    redis_admin_url connects as the redis `default` (admin) user, whose password is
    the cluster secret (each cluster's redis is single-tenant, so `requirepass` == the
    cluster secret)."""
    import redis

    # redis-py types from_url's **kwargs as Unknown; the call itself is fully typed.
    client = redis.Redis.from_url(redis_admin_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    try:
        # redis-py types execute_command()'s signature as partially Unknown; the call is fully typed.
        client.execute_command(  # pyright: ignore[reportUnknownMemberType]
            "ACL",
            "SETUSER",
            user,
            "on",
            "resetpass",
            f">{cluster_secret}",
            "resetkeys",
            "~*",
            "resetchannels",
            f"&{channel_prefix}:*",
            "+@all",
            "-@dangerous",
        )
    finally:
        client.close()


def derive_env(
    rec: cluster.ClusterRecord,
    *,
    base_db_url: str,
    base_redis_url: str,
    cluster_secret: str,
    pgbouncer_enabled: bool = True,
) -> dict[str, str]:
    """Map a cluster record to the env vars a unit needs. Daemons read these via
    settings, so the cluster layer touches no daemon code.

    `cluster_secret` is written into the cluster's `.env` two ways: as
    `AVA_CLUSTER_SECRET` itself (so every cluster process reads it from the
    cluster home even when nothing is inherited), and embedded in
    `AVA_DB_URL` / `AVA_REDIS_URL`. Both URLs carry the cluster's data-plane
    identity — the fixed `DATA_PLANE_IDENTITY` db/role/ACL user — **as data**:
    every consumer reads the identity back from these URLs; nothing re-derives
    it from a name. Settings re-applies only the PASSWORD on load, so a rotated
    secret self-heals while the identity stays whatever the `.env` says. An
    empty secret still writes the identity username — names-as-data holds
    without auth. Pub/sub channels are fixed (`ava:*`).

    `AVA_DB_URL` is the ONE access URL every process dials as-is;
    `pgbouncer_enabled` decides its port at generation — pooler (default) or
    direct Postgres. No pgbouncer-port env key."""
    p = rec.ports
    db_url = url_with_userinfo(
        cluster._swap_db(base_db_url, cluster.DATA_PLANE_IDENTITY),
        cluster.DATA_PLANE_IDENTITY,
        cluster_secret,
    )
    if pgbouncer_enabled:
        db_url = url_with_port(db_url, cluster.record_pgbouncer_port(rec))
    env = {
        "AVA_CLUSTER_SECRET": cluster_secret,
        "AVA_GATEWAY_PORT": str(p["gateway"]),
        # A gateway box reaches its OWN gateway over loopback (same-machine call);
        # the address remote agent-runners dial is handed to them out-of-band at
        # `ava enroll`, never stored here. Materialized so `ava start` reads the URL
        # from .env with no runtime default (an enrolled runner overwrites this with
        # the gateway's reachable URL).
        "AVA_GATEWAY_URL": f"http://localhost:{p['gateway']}",
        "AVA_GATEWAY_HEALTH_URL": f"http://localhost:{p['gateway']}/api/health",
        "AVA_FRONTEND_HEALTHCHECK_URL": f"http://localhost:{p['frontend']}",
        "AVA_APP_PORT": str(cluster.record_app_port(rec)),
        "AVA_MILVUS_PORT": str(p["milvus"]),
        "AVA_MILVUS_URI": f"http://127.0.0.1:{p['milvus']}",
        "AVA_BROWSER_CDP_PORT": str(p["browser"]),
        "AVA_PERMISSIONS_HELPER_PORT": str(cluster.record_health_port(rec, "permissions_helper")),
        "AVA_DB_URL": db_url,
        "AVA_REDIS_URL": url_with_userinfo(
            base_redis_url, cluster.DATA_PLANE_IDENTITY, cluster_secret
        ),
        "AVA_EVENTS_CHANNEL": "ava:events",
    }
    # p is a ClusterPorts (all int values). Use record_health_port so old records
    # that predate a health-port slot still resolve their port deterministically.
    for svc, var in health_port_env_aliases().items():
        env[var] = str(cluster.record_health_port(rec, svc))
    return env


# The derived/identity key sets live in shared/env_registry.py (the R2 env
# registry): shared.dotenv_boot imports them at config-load time without a
# cycle. derive_env's output surface is `derived_env_keys()` there.


def per_cluster_base_urls(rec: cluster.ClusterRecord) -> tuple[str, str]:
    """The base `(db_url, redis_url)` for a cluster's own instance — loopback at the
    cluster's allocated pg/redis ports. `derive_env` swaps in the db name and the
    data-plane identity + secret on top."""
    return (
        f"postgresql://x@127.0.0.1:{rec.ports['postgres']}/postgres",
        f"redis://127.0.0.1:{rec.ports['redis']}/0",
    )
