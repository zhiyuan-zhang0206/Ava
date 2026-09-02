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
from shared.env_registry import REDIS_PASSWORD_ENV, health_port_env_aliases
from shared.platform import IS_WINDOWS
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
# `ava cluster ensure-db-role`, written to the gateway's .env as
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
    (ui/web/src/lib/api.ts), correct on any host whose gateway is not on the
    default 8000 (e.g. the prod VPS on 8800, co-located with another service
    holding 8000).

    Single source for both build paths — the canonical ServiceSpec
    (cli/commands/_repo.py) AND the frontend healthcheck respawn
    (services/healthchecks/frontend.py) — so a watchdog restart can never bake a
    different (stale) gateway port than `ava start` did.
    """
    return f"NEXT_PUBLIC_GATEWAY_PORT={settings.gateway.gateway_port}"


def frontend_service_cmd(port: int, frontend_dir: str | Path = "ui/web") -> str:
    """The complete frontend service launch command — single source for BOTH
    launch paths: the canonical ServiceSpec (``ops/spec.py``) and the watchdog
    respawn (``services/healthchecks/frontend.py``). The two drifted once
    (2026-08-27 prod outage: the respawn command lost its ``exec``, so the
    session validator rejected it and a dead frontend could never self-heal);
    this is the one place the command shape is authored.

    ``exec`` on the serve stage (POSIX): the build is a transient prelude, so
    the shell must hand its pid to ``npm run start`` — otherwise it outlives it
    and swallows the graceful-stop SIGTERM (``shared.session_env.exec_into``
    rejects a compound command whose final stage does not exec, which is what
    made the drifted respawn unlaunchable). On Windows cmd.exe has no ``exec``;
    the ``&&`` chain runs through ``cmd /c`` and winproc kills the process tree,
    so the Windows shape carries ``set "VAR=val"`` instead of a bash inline env
    prefix (cmd cannot do ``VAR=val cmd``).

    Args:
        port: the cluster-allocated frontend port. Passed explicitly — Next.js
            defaults to 3000 otherwise, and a watchdog restart would silently
            revert the app off-cluster.
        frontend_dir: the directory to ``cd`` into before the build, as the
            session sees it — the spec's session starts at the checkout root
            (``ui/web``), the respawn's at the absolute ``ui/web`` path. The
            build env prefix rides the command (NEXT_PUBLIC_* is build-time
            inlined and never reaches the build from a unit .env); see
            ``fe_build_env``.
    """
    frontend_dir = Path(frontend_dir).as_posix()
    build_env = fe_build_env()
    if IS_WINDOWS:
        return (
            f'cd {frontend_dir} && set "{build_env}" && npm run build && npm run start -- -p {port}'
        )
    return f"cd {frontend_dir} && {build_env} npm run build && exec npm run start -- -p {port}"


def redis_admin_url() -> str:
    """libpq-style redis URL connecting as the `default` (admin) user, used to
    provision the cluster's redis ACL user + run admin-plane probes. Not a
    per-cluster runtime identity.

    Every cluster owns its Redis instance. Its `default` user password is an
    independent gateway-only credential; a pre-split .env temporarily falls
    back to the bearer until `ava start` mints the split. Host/port come from
    this cluster's own `redis_url` (loopback + its per-cluster port), never a
    hardcoded 6379."""
    parts = urlsplit(settings.data_plane.redis_url)
    # `parts.port` is None when the URL carries no explicit port — the
    # stringified ":None" would be a connect error with a confusing message
    # (audit 2026-08-08 P3: settings normally carry the port, this is the
    # defensive floor).
    port = parts.port or 6379
    password = settings.data_plane.redis_admin_password or settings.data_plane.cluster_secret
    return f"redis://default:{password}@{parts.hostname or '127.0.0.1'}:{port}"


def redis_password_from_env() -> str:
    """This gateway home's file-only Redis ACL runtime password, or an empty
    string when a legacy cluster has not completed the split mint step."""
    from dotenv import dotenv_values

    from shared.paths import ava_home

    return (dotenv_values(ava_home() / ".env").get(REDIS_PASSWORD_ENV) or "").strip()


def runner_password_from_env(home: Path | None = None) -> str:
    """Read a gateway home's runner DB password from its `.env` file.

    The runner credential is deliberately not a Settings field: it is gateway
    secret material that bootstrap projects only inside the runner database URL.
    ``home`` exists for the pooler, which may reconcile a specified home; normal
    callers read this process's checkout-anchored home.
    """
    from dotenv import dotenv_values

    from shared.paths import ava_home

    env_path = (home if home is not None else ava_home()) / ".env"
    return (dotenv_values(env_path).get(RUNNER_DB_PASSWORD_ENV) or "").strip()


def runner_db_url_projection(db_url: str) -> str:
    """Project ``db_url`` onto the least-privilege runner identity.

    Agent processes and agent-profile daemons receive the runner credential
    inside their database URL, never as a standalone secret. Keep an already
    projected URL byte-for-byte: it may have arrived from a remote gateway and
    carries the runner role's current password.
    """
    from shared.config.data_plane import _is_runner_db_url

    if _is_runner_db_url(db_url):
        return db_url
    runner_password = cluster.runner_password_from_env()
    if not runner_password:
        raise RuntimeError(
            "AVA_RUNNER_DB_PASSWORD is not set in the gateway's .env — run "
            "`ava cluster ensure-db-role` before spawning agents."
        )
    return url_with_userinfo(db_url, RUNNER_ROLE, runner_password)


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


def derive_env(
    rec: cluster.ClusterRecord,
    *,
    base_db_url: str,
    base_redis_url: str,
    cluster_secret: str,
    db_admin_password: str = "",
    redis_admin_password: str = "",
    redis_password: str = "",
    pgbouncer_enabled: bool = True,
) -> dict[str, str]:
    """Map a cluster record to the env vars a unit needs. Daemons read these via
    settings, so the cluster layer touches no daemon code.

    `cluster_secret` is written as `AVA_CLUSTER_SECRET`, the control-plane
    bearer every enrolled runner needs. `AVA_DB_URL` instead carries the
    gateway-local Postgres owner password and `AVA_REDIS_URL` the runtime ACL
    password. Both URLs carry the fixed `DATA_PLANE_IDENTITY` db/role/ACL user
    **as data**: every consumer reads the identity back from these URLs; nothing
    re-derives it from a name. The three data-plane passwords are independently
    persisted so their rotation self-heals the matching URLs without changing
    the bearer. An empty secret writes empty data-plane passwords and still
    retains the identity username — names-as-data holds without auth. Pub/sub
    channels are fixed (`ava:*`).

    `AVA_DB_URL` is the ONE access URL every process dials as-is;
    `pgbouncer_enabled` decides its port at generation — pooler (default) or
    direct Postgres. No pgbouncer-port env key."""
    p = rec.ports
    db_password = db_admin_password or cluster_secret
    redis_default_password = redis_admin_password or cluster_secret
    runtime_password = redis_password or cluster_secret
    db_url = url_with_userinfo(
        cluster._swap_db(base_db_url, cluster.DATA_PLANE_IDENTITY),
        cluster.DATA_PLANE_IDENTITY,
        db_password,
    )
    if pgbouncer_enabled:
        db_url = url_with_port(db_url, cluster.record_pgbouncer_port(rec))
    env = {
        "AVA_CLUSTER_SECRET": cluster_secret,
        "AVA_DB_ADMIN_PASSWORD": db_password,
        "AVA_REDIS_ADMIN_PASSWORD": redis_default_password,
        REDIS_PASSWORD_ENV: runtime_password,
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
        # Late slot: derived via record_memory_search_port (old records fall
        # back to the legacy 19531 their .env also binds).
        "AVA_MEMORY_SEARCH_PORT": str(cluster.record_memory_search_port(rec)),
        "AVA_MEMORY_SEARCH_URI": f"http://127.0.0.1:{cluster.record_memory_search_port(rec)}",
        "AVA_BROWSER_CDP_PORT": str(p["browser"]),
        "AVA_PERMISSIONS_HELPER_PORT": str(cluster.record_health_port(rec, "permissions_helper")),
        "AVA_DB_URL": db_url,
        "AVA_REDIS_URL": url_with_userinfo(
            base_redis_url, cluster.DATA_PLANE_IDENTITY, runtime_password
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
    """The base `(db_url, redis_url)` for a cluster's own instance — loopback at
    the cluster's allocated pg/redis ports by default, or the record's
    `data_plane_host` when one is set (external data plane, Task #1752). The
    host source is the registry record, never a hardcoded literal, so an
    off-box data plane changes only the record/settings, not this derivation.
    `derive_env` swaps in the db name, data-plane identity, and independently
    scoped passwords on top."""
    host = (rec.data_plane_host or "").strip() or "127.0.0.1"
    return (
        f"postgresql://x@{host}:{rec.ports['postgres']}/postgres",
        f"redis://{host}:{rec.ports['redis']}/0",
    )
