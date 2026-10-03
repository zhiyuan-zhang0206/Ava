"""Derived cluster facts — identity, labels, session names, channels, env.

Everything a cluster derives rather than stores: the data-plane identity read
from `.env` URLs as data (`identity_from_url` / `db_identity` /
`redis_identity`, plus the fixed `DATA_PLANE_IDENTITY` at birth); the
home-derived display label and short-path token (`home_label` / `home_slug`);
the session-name composer (`session_name`); the redis
channel/wake-key names and admin URL; and
`derive_env` — the record-to-env mapping a unit needs (plus its base-URL
helper `per_cluster_base_urls`).
"""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from base import cluster
from base.host.env.registry import REDIS_PASSWORD_ENV, health_port_env_aliases
from base.host.net.url_secret import redacted_url, url_with_port, url_with_userinfo
from base.native_process.os_platform import IS_WINDOWS

# The db / Postgres-role / redis-ACL identifier a newly-born cluster uses. Fixed:
# every cluster owns its instance (exactly one tenant), so the identifier carries
# no cluster distinction. Existing clusters keep whatever identifier their `.env`
# URLs carry (names-as-data) until an ops rename.
DATA_PLANE_IDENTITY = "ava"

# The provider-provisioned runner role's FIXED name and the gateway-.env key
# carrying its password, for REMOTE-MANAGED data planes only (write generations
# are unsupported there until an operator-declared provider admin authority
# exists). A local plane's runners log in as its write generation's runner
# login instead (`base.cluster.authority`); nothing local mints this password.
RUNNER_ROLE = "ava_runner"
RUNNER_DB_PASSWORD_ENV = "AVA_RUNNER_DB_PASSWORD"  # noqa: S105 — the env KEY name, not a credential


def default_home() -> Path:
    """The default (prod) cluster home, `~/.ava` — the one home whose initialization
    uses the fixed default port block and whose checkout anchors without a
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
    basename). Used where a per-home token must live outside the home itself: the
    short pg socket dir under /tmp, and the permissions helper's launchd label."""
    base = Path(home).expanduser().name.lstrip(".") or "home"
    digest = hashlib.sha1(str(Path(home).expanduser()).encode(), usedforsecurity=False).hexdigest()[
        :8
    ]
    return f"{base}-{digest}"


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
            f"data-plane URL carries no username (the db/role/ACL identity): "
            f"{redacted_url(url)!r}. Identity is read from the URL as data, never "
            "guessed — fix the .env URL (e.g. postgresql://ava:<secret>@host:port/ava, "
            "redis://ava:<secret>@host:port/0)."
        )
    return user


def db_identity() -> str:
    """This cluster's Postgres database / schema-owner identity, read from its own
    db_url's DATABASE name.

    The database and its NOLOGIN owner share the identifier (provisioning
    creates `DATABASE <identity> OWNER <identity>`). The database is read rather
    than the username because a delivered write-generation login
    (`ava_g0_<class>`) replaces the URL's username, never its database.

    Raises:
        ValueError: db_url names no database."""
    from base.config import settings

    url = settings.data_plane.db_url
    database = urlsplit(url).path.strip("/")
    if not database:
        raise ValueError(
            f"data-plane database URL names no database (the db/owner identity): "
            f"{redacted_url(url)!r}. Identity is read from the URL as data, never guessed."
        )
    return database


def redis_identity() -> str:
    """This cluster's redis ACL user, read from its own redis_url.

    Raises:
        ValueError: redis_url carries no username (see identity_from_url)."""
    from base.config import settings

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
    from base.config import settings

    gateway_env = f"NEXT_PUBLIC_GATEWAY_PORT={settings.gateway.gateway_port}"
    if not (origin := settings.gateway.browser_origin):
        return gateway_env
    # The normal build and watchdog rebuild must carry the same browser entry.
    # Keep this host setting separate from the runner/control-plane gateway URL.
    if IS_WINDOWS:
        return f'{gateway_env}" && set "NEXT_PUBLIC_BROWSER_ORIGIN={origin}'
    return f"{gateway_env} NEXT_PUBLIC_BROWSER_ORIGIN={shlex.quote(origin)}"


def frontend_service_cmd(port: int, frontend_dir: str | Path = "ui/web") -> str:
    """The complete frontend service launch command — single source for BOTH
    launch paths: the canonical ServiceSpec (``ops/spec.py``) and the watchdog
    respawn (``services/healthchecks/frontend.py``). The two drifted once
    (2026-08-27 prod outage: the respawn command lost its ``exec``, so the
    session validator rejected it and a dead frontend could never self-heal);
    this is the one place the command shape is authored.

    ``exec`` on the serve stage (POSIX): the build is a transient prelude, so
    the shell must hand its pid to ``npm run start`` — otherwise it outlives it
    and swallows the graceful-stop SIGTERM (``base.sessions.env_forwarding.exec_into``
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
    independent gateway-only credential (`AVA_REDIS_ADMIN_PASSWORD`, also the
    instance's `requirepass`). Host/port come from this cluster's own
    `redis_url` (loopback + its per-cluster port), never a hardcoded 6379."""
    from base.config import settings

    parts = urlsplit(settings.data_plane.redis_url)
    # `parts.port` is None when the URL carries no explicit port — the
    # stringified ":None" would be a connect error with a confusing message
    # (audit 2026-08-08 P3: settings normally carry the port, this is the
    # defensive floor).
    port = parts.port or 6379
    password = settings.data_plane.redis_admin_password
    return f"redis://default:{password}@{parts.hostname or '127.0.0.1'}:{port}"


def redis_password_from_env() -> str:
    """This gateway home's file-only Redis ACL runtime password.

    Empty only on a home born before Redis always authenticated; storage
    bring-up refuses it."""
    from dotenv import dotenv_values

    from base.paths import ava_home

    return (dotenv_values(ava_home() / ".env").get(REDIS_PASSWORD_ENV) or "").strip()


def runner_db_url_projection() -> str:
    """The provider runner login for agents on a remote-managed gateway plane.

    A remote-managed (provider) plane has no write generation: its agents dial
    the provider-provisioned `ava_runner` with the gateway `.env`'s recorded
    provider credential, read with its URL from one current file snapshot, never
    a cached URL combined with a newly rotated password. Only a gateway-config
    unit projects this; a pure agent-runner's services receive their installed
    unit capability instead (`base.cluster.authority.unit`).
    """
    from base.host.env.bootstrap import config_source_is_local
    from base.host.env.runtime_config import read_env_aliases

    if not config_source_is_local():
        raise RuntimeError(
            "a pure agent-runner never projects a runner login; its services receive the "
            "installed unit capability"
        )
    aliases = read_env_aliases()
    snapshot_url = aliases.get("AVA_DB_URL")
    if not snapshot_url:
        raise RuntimeError("AVA_DB_URL is missing from the gateway config snapshot")
    return project_runner_db_url(snapshot_url, aliases.get(RUNNER_DB_PASSWORD_ENV) or "")


def project_runner_db_url(db_url: str, runner_password: str) -> str:
    """Pure credential projection; both inputs must belong to one config snapshot."""
    if not runner_password:
        raise RuntimeError(
            "AVA_RUNNER_DB_PASSWORD is missing from the gateway initialization; "
            "restore its recorded credential before starting services or agents."
        )
    return url_with_userinfo(db_url, RUNNER_ROLE, runner_password)


def redis_channel_prefix() -> str:
    """The pub/sub channel prefix (`ava`) — the events channel is `<prefix>:events`,
    so strip that suffix. Fixed across clusters now that each owns its redis: there
    is no neighbour to prefix away from."""
    from base.config import settings

    return settings.data_plane.events_channel.removesuffix(":events")


def inbound_channel(agent_id: int) -> str:
    """The Redis pub/sub channel an agent waits on for inbound wake-ups —
    `ava:inbound:<agent_id>`, via `redis_channel_prefix()`.

    Publish (`base.db.insert_inbound_message` / `insert_compact_request_inbound`,
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
    redis_admin_password: str,
    redis_password: str,
    pgbouncer_enabled: bool = True,
) -> dict[str, str]:
    """Map a cluster record to the env vars a unit needs. Daemons read these via
    settings, so the cluster layer touches no daemon code.

    `cluster_secret` is written as `AVA_CLUSTER_SECRET`, the control-plane
    bearer every enrolled runner needs. `AVA_DB_URL` is the CREDENTIAL-FREE
    endpoint (`postgresql://<identity>@host:port/<identity>`): the schema owner
    is NOLOGIN, and every process dials as a write-generation login its launcher
    delivers (`base.cluster.authority`), never with a password from `.env`.
    `AVA_REDIS_URL` carries the runtime ACL password. Both URLs carry the fixed
    `DATA_PLANE_IDENTITY` db/role/ACL user **as data**: every consumer reads the
    identity back from these URLs; nothing re-derives it from a name. Redis
    always authenticates, so both Redis passwords are required whatever the
    bearer. Pub/sub channels are fixed (`ava:*`).

    `AVA_DB_URL` is the ONE access endpoint; `pgbouncer_enabled` decides its port
    at generation — pooler (default) or direct Postgres. No pgbouncer-port env key."""
    p = rec.ports
    if not (redis_admin_password and redis_password):
        raise ValueError(
            "identity requires explicit data-plane credentials: Redis always "
            "authenticates with its admin and runtime passwords"
        )
    redis_default_password = redis_admin_password
    runtime_password = redis_password
    db_url = url_with_userinfo(
        cluster._swap_db(base_db_url, cluster.DATA_PLANE_IDENTITY),
        cluster.DATA_PLANE_IDENTITY,
        "",
    )
    if pgbouncer_enabled:
        db_url = url_with_port(db_url, p["pgbouncer"])
    env = {
        "AVA_CLUSTER_SECRET": cluster_secret,
        "AVA_REDIS_ADMIN_PASSWORD": redis_default_password,
        REDIS_PASSWORD_ENV: runtime_password,
        "AVA_GATEWAY_PORT": str(p["gateway"]),
        # A gateway box reaches its OWN gateway over loopback (same-machine call);
        # the address remote agent-runners dial is handed to them out-of-band at
        # runner first start, never stored here. Materialized so `ava start` reads the URL
        # from .env with no runtime default (an enrolled runner overwrites this with
        # the gateway's reachable URL).
        "AVA_GATEWAY_URL": f"http://localhost:{p['gateway']}",
        "AVA_GATEWAY_HEALTH_URL": f"http://localhost:{p['gateway']}/api/health",
        "AVA_FRONTEND_HEALTHCHECK_URL": f"http://localhost:{p['frontend']}",
        "AVA_APP_PORT": str(p["app"]),
        "AVA_MEMORY_SEARCH_PORT": str(p["memory_search"]),
        "AVA_MEMORY_SEARCH_URI": f"http://127.0.0.1:{p['memory_search']}",
        "AVA_BROWSER_CDP_PORT": str(p["browser"]),
        "AVA_PERMISSIONS_HELPER_PORT": str(p["permissions_helper"]),
        "AVA_DB_URL": db_url,
        "AVA_REDIS_URL": url_with_userinfo(
            base_redis_url, cluster.DATA_PLANE_IDENTITY, runtime_password
        ),
        "AVA_EVENTS_CHANNEL": "ava:events",
    }
    # The health-port services are a subset of the closed ClusterPorts keys.
    by_service = cast("dict[str, int]", p)
    for svc, var in health_port_env_aliases().items():
        env[var] = str(by_service[svc])
    return env


# The derived/identity key sets live in base/host/env/registry.py (the R2 env
# registry): base.host.env.dotenv_boot imports them at config-load time without a
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
