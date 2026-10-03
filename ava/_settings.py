"""Internal config — DB / Redis lazy proxy.

The agent accesses these directly via `ava.DB` / `ava.REDIS`; they don't
appear in `ava.help()` output. (The agent's own id lives at `ava.self.AGENT_ID`.)

DB and REDIS are lazy proxies — no connection on import, first method
call triggers connection. In container mode (no Postgres / Redis),
import ava doesn't blow up; only when ava DB ops are actually used does
it raise.

URL source: `base.config.settings` single source of truth. Settings'
infra-pointing fields (db_url / redis_url) have no default; when env is
missing, Settings() instantiation throws ValidationError immediately,
not reaching here.
"""

import sys
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any

from base.config import settings

# DB_URL / REDIS_URL / GATEWAY_URL are exposed via module __getattr__ (PEP
# 562) so each access reads the current `settings.X` value rather than a
# load-time snapshot. This removes the conftest "must mutate settings before
# this module is imported" invariant: tests can flip settings.data_plane.db_url at any
# point and the next ava DB op picks it up.


def __getattr__(name: str) -> Any:
    if name == "DB_URL":
        return settings.data_plane.db_url
    if name == "REDIS_URL":
        return settings.data_plane.redis_url
    if name == "GATEWAY_URL":
        from base.cluster.machine import gateway_api_base

        return gateway_api_base()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _conn_dead(conn: object) -> bool:
    """True when psycopg has observed the connection's death.

    `closed` / `broken` are psycopg Connection properties (both bool); the
    getattr fallbacks keep the proxy safe for any future factory that returns
    a non-psycopg object exposing neither attribute.
    """
    closed = getattr(conn, "closed", False)
    if closed:
        return True
    broken = getattr(conn, "broken", False)
    return bool(broken)


class _LazyConnection:
    """On-demand connection proxy — attribute access forwarded to the real connection.

    No connection triggered on import ava (container mode / tests-without-db
    don't need a real DB); first `ava.DB.cursor(...)` triggers the factory
    to build a connection.

    A dead connection is rebuilt on next access: `closed` / `broken` are
    checked before every use, so a network outage that kills the socket
    (psycopg marks the conn broken after a failed read/write) cannot wedge the
    proxy permanently — the next call gets a fresh connection instead of
    reusing the dead one. Without this, every ava.DB op in the process fails
    until restart (agent 2147: pause_heartbeat failed for hours after the
    network returned, while the agent main-loop pool — which checks every
    borrow — self-healed). The check happens on access, so a half-open
    connection whose death psycopg has not yet observed still fails the first
    call after the outage (bounded by PG_KEEPALIVE_KWARGS); that call is the
    one that marks it broken, and the next access rebuilds.
    """

    def __init__(self, factory: Callable[[], object], name: str) -> None:
        self._factory = factory
        self._name = name
        self._conn: object | None = None
        self._lock = threading.Lock()

    def _get(self) -> object:
        with self._lock:
            conn = self._conn
            if conn is not None and not _conn_dead(conn):
                return conn
            if conn is not None:
                with suppress(Exception):
                    conn.close()  # type: ignore[attr-defined]
            self._conn = self._factory()
            return self._conn

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._get(), attr)

    def __repr__(self) -> str:
        if self._conn is None:
            return f"<lazy {self._name} (not connected)>"
        return repr(self._conn)


# The SDK's composition root: the handles and the agent configuration every `ava.*` function of
# this process works with. `ava.*` is a namespace of free functions the agent's code calls inside
# its exec child (or a script one launched), so nothing can pass them a handle; they ask here. The
# exec child's settings carry its agent's overlay (applied at boot), so `agent_setting` reads that
# agent's; a process attached to an agent's native state reads that agent's pins. Nothing is cached: each call builds from the
# settings as they are now. All import lazily — `import ava` must not pull the psycopg / redis /
# live-events stacks into every exec child (task #3816).


def database() -> "Database":  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """The cluster database, as this process's settings name it."""
    from base.db import Database

    return Database.from_settings()


def bus() -> "EventBus":  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """The live event bus, as this process's settings name it."""
    from base.events.live.bus import EventBus

    return EventBus.from_settings()


def shell_sessions() -> "ShellSessions":  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """This agent's persistent shell sessions, on the shell backend and the cluster database.

    Raises RuntimeError when this process has no agent identity: shell sessions are an agent's,
    so a standalone script that imports ava gets an explicit refusal, never another agent's or
    a global's sessions."""
    import ava.agent_identity
    from ava.shell.sessions import ShellSessions
    from base.sessions.backend import get_shell_backend

    agent_id = ava.agent_identity.agent_id()
    if agent_id is None:
        raise RuntimeError(
            "Cannot use shell sessions: this process has no agent identity. "
            "ava.shell.sessions requires an agent process or a background "
            "script launched by one (which receives the identity via "
            "ava.agent_identity.establish). Running a standalone script that imports ava "
            "does not set an agent identity."
        )
    return ShellSessions(backend=get_shell_backend(), database=database(), agent_id=agent_id)


def page_host() -> "PageHost":  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """The page host this agent's `ava.ui` calls run against: this machine's reachable host and
    the agent's identity (RuntimeError when the process has none), read per call."""
    import ava.agent_identity
    from ava.ui import PageHost
    from base.cluster.machine import reachable_host

    return PageHost(host=reachable_host(), agent_id=ava.agent_identity.require_agent_id())


def _attached() -> tuple[Mapping[str, Any], Any] | None:
    """The pins and plugin-config view of the agent this process attached to (`ava.external`:
    one attachment per process), if any."""
    external = sys.modules.get("ava.external")
    return external.attached_config() if external is not None else None


def agent_setting(name: str) -> Any:
    """One per-agent setting of this process's agent: the attached agent's pin, else the
    settings (which carry the exec child's overlay)."""
    from base.host.env.agent_slices import agent_setting

    attached = _attached()
    return agent_setting(name, attached[0] if attached else None)


def _connect_db() -> "psycopg.Connection":  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    if not settings.data_plane.db_url:
        raise RuntimeError("AVA_DB_URL not set — ava DB ops should not be called in container mode")
    # autocommit=True: every op at the SDK layer is a single-statement
    # INSERT/SELECT, no multi-statement transaction need. autocommit keeps
    # the connection from staying in a transaction state, avoiding:
    # 1. SELECT leaving 'idle in transaction' blocking ACCESS EXCLUSIVE
    #    locks on other connections
    # 2. SQL errors putting conn into INERROR state, with subsequent SDK
    #    calls all failing in a chain (especially hard to diagnose when
    #    caller forgets rollback)
    # Multi-statement transactions don't go through this conn — the caller
    # opens its own connection.
    #
    # base.db.connect() owns the rest of the posture, each part load-bearing
    # here: `ava.DB` is dialled from inside the agent's exec sandbox, so the 5s
    # connect cap keeps a database that black-holes packets from freezing the
    # agent's tool call; this connection lives for the whole process while
    # pgbouncer hands each transaction a possibly different backend, so it never
    # prepares server-side (2026-09-21: a watcher's poll wedged on `_pg3_0`);
    # and every (re)connect scrubs the pooled session back to baseline, so
    # another client's session-level SET (2026-09-02 P0 read-only pollution)
    # cannot break this connection's writes.
    return database().connect(autocommit=True)


def _connect_redis() -> "redis.Redis":  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    import redis as _redis_lib

    from base.events.live.redis_client import RESILIENCE_KWARGS

    if not settings.data_plane.redis_url:
        raise RuntimeError(
            "AVA_REDIS_URL not set — ava Redis ops should not be called in container mode"
        )
    # The same weak-network posture as the shared clients: connect timeout,
    # TCP keepalive + health_check bound a half-dead socket in tens of
    # seconds instead of the OS TCP-retransmit timeout. Unlike the shared
    # clients, socket_timeout IS set here: ava.REDIS serves short SDK
    # get/set/publish from the exec sandbox (no pubsub long-read — the
    # inbound listener uses its own aredis client), so a 10s read bound is a
    # hard floor under keepalive (which takes ~60s to declare a peer dead).
    # Audit #689 G3 (Task #690 timeout ruling).
    return _redis_lib.Redis.from_url(
        settings.data_plane.redis_url,
        decode_responses=True,
        # Merge so the explicit 10s read bound overrides the shared None
        # instead of colliding with it as a duplicate keyword.
        **{**RESILIENCE_KWARGS, "socket_timeout": 10.0},
    )


DB = _LazyConnection(_connect_db, "DB")
REDIS = _LazyConnection(_connect_redis, "REDIS")


# ── Plugin config hierarchical view ──
#
# `ava._settings.plugins.<plugin_name>` dynamically resolves the frozen Pydantic
# BaseModel instance for the current turn's agent (bound in by
# the SDK install from the plugin's declared config; an attached agent's overrides by
# `base/packages/plugins/config_view.py`).
#
# Design:
# - Private module (underscore prefix) → not in `ava.help()`, for plugin authors not the agent
# - lazy attribute access → no cache here, so restart / test monkeypatch changes
#   to the registry are immediately visible
# - lazy import base.packages.plugins.config_registration → avoids ava module load triggering agent
#   module import (test fixture / container mode can still import ava
#   without connecting agent)


class _PluginsView:
    """`ava._settings.plugins` — attribute access routes to the turn's config
    for that plugin (`base/packages/plugins/config_view.py`).

    Plugins not registered raise AttributeError listing known plugin names,
    so typos / "bind hasn't run yet" are immediately visible.
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        from base.packages.plugins.config_registration import (
            process_plugin_config,
            registered_plugin_config_names,
        )

        known = registered_plugin_config_names()
        if name not in known:
            raise AttributeError(
                f"ava._settings.plugins.{name} does not exist — plugin {name!r} declares no "
                f"config, or the SDK surface is not installed yet. "
                f"Known plugins: {known or '<empty>'}"
            )
        # The attached agent's overrides over the disk image; otherwise this process's own
        # instance (the exec child's boot applied its agent's overlay to it).
        attached = _attached()
        return attached[1].config_for(name) if attached else process_plugin_config(name)


plugins = _PluginsView()
