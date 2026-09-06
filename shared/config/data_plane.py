"""Data plane config — DataPlaneSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from shared.config._base import EnvSettings, _unit_home
from shared.dotenv_boot import UNANCHORED_DB_SENTINEL
from shared.netutil import is_ipv4_literal, is_loopback_host
from shared.url_secret import url_host, url_with_host, url_with_password, url_with_query_param


def _self_machine_host() -> str:
    """This host's reachable address, mirroring `shared.machine.reachable_host`
    (env `AVA_MACHINE_HOST` > `$AVA_HOME/machine_host` file > `localhost`).
    Duplicated at this leaf because shared.machine imports settings — a config
    sub-model cannot import it back. `load_ava_env` pins AVA_HOME into os.environ
    before any sub-model constructs, so the file branch resolves against the same
    home the `.env` came from."""
    env = os.environ.get("AVA_MACHINE_HOST", "").strip()
    if env:
        return env
    path = Path(os.environ.get("AVA_HOME", "~/.ava")).expanduser() / "machine_host"
    if path.exists():
        host = path.read_text().strip()
        if host:
            return host
    return "localhost"


def _is_runner_db_url(url: str) -> bool:
    """Whether `url` carries the least-privilege `ava_runner` identity.

    Read as data from the URL's username (the same names-as-data read every
    consumer uses), never from a cluster name. The runner role is FIXED — it is
    part of the bootstrap projection contract (GET /api/bootstrap?role=runner),
    not a per-cluster fact. The literal mirrors `shared.cluster.derive.RUNNER_ROLE`;
    duplicated at this leaf because shared.cluster imports shared.config.settings
    and this module runs DURING the Settings build (the same reason
    `_self_machine_host` duplicates reachable_host)."""
    return urlsplit(url).username == "ava_runner"


def _loopback_if_self(url: str) -> str:
    """Return `url` with its host swapped to `127.0.0.1` when it names this
    machine's own reachable address; any other host — and an already-loopback
    host — passes through verbatim. The unanchored sentinel is skipped
    explicitly (the connect guard matches it byte-for-byte).

    External-migration semantics (Task #1752): this rewrite is the SELF-DIAL
    posture only. Once the data plane is hosted off-box (a foreign
    `data_plane_host` at birth), its URLs name another machine and pass
    through here untouched — the rewrite must never be extended to foreign
    hosts, because its job is the opposite: keep this box's own traffic on
    loopback, not route it to where the URL happens to point."""
    if url == UNANCHORED_DB_SENTINEL:
        return url
    host = urlsplit(url).hostname or ""
    if not host or is_loopback_host(host):
        return url
    machine = _self_machine_host().strip().lower().removeprefix("[").removesuffix("]")
    if host == machine:  # urlsplit lowercases + unbrackets hostname; match that form
        return url_with_host(url, "127.0.0.1")
    return url


def sslmode_for_url(url: str, configured: str) -> str | None:
    """The libpq `sslmode` kwarg for a dial of `url`, or None when the dial
    should carry none.

    None means "leave TLS to the URL and libpq": the URL already names an
    sslmode query parameter (the URL is the switch — a SaaS connection string
    carries its own `sslmode=require` / `verify-full` and must win), or
    `configured` (AVA_DB_SSLMODE) is empty — libpq's own default, 'prefer',
    applies, so a local loopback dial against a non-TLS PgBouncer/Postgres
    stays plaintext exactly as today.

    A configured AVA_DB_SSLMODE is injected only when the URL is silent, so
    the config field is a fallback default, never an override. psycopg merges
    kwargs over the URL's conninfo params, so injecting here when the URL
    already names a mode would silently downgrade a stricter URL mode.
    """
    from urllib.parse import parse_qs

    if "sslmode" in parse_qs(urlsplit(url).query):
        return None
    return configured.strip() or None


def resolved_pool_size(
    min_size: int | None, max_size: int | None, default_min: int, default_max: int
) -> tuple[int, int]:
    """Resolve explicit pool sizes against the config defaults
    (AVA_DB_POOL_MIN_SIZE / AVA_DB_POOL_MAX_SIZE): an explicit caller value
    wins, None falls back to the defaults. A remote/SaaS data plane tunes its
    pool footprint from config without code changes (Task #1752)."""
    return (
        min_size if min_size is not None else default_min,
        max_size if max_size is not None else default_max,
    )


def gateway_url_host() -> str:
    """The host this process dials as its gateway (`AVA_GATEWAY_URL`), lowercased
    and unbracketed; "" when the gateway domain is not constructed in this
    process's profile (or unset). Used by `shared.db.direct_db_url` to tell a
    split agent-runner's URL — which names the GATEWAY's pooler, a foreign host
    whose direct-exemption warning is meaningful — apart from a remote/SaaS
    plane URL, which is direct by definition and must dial silently
    (Task #1752)."""
    from shared.config import settings

    try:
        url = settings.gateway.gateway_url
    except AttributeError:
        return ""
    return (urlsplit(url).hostname or "").lower().removeprefix("[").removesuffix("]")


class DataPlaneSettings(EnvSettings):
    db_url: str = Field(
        alias="AVA_DB_URL",
        description="The cluster's ONE database access URL, dialed as-is by every "
        "process. Its port is chosen at URL generation (install / converge) by "
        "AVA_PGBOUNCER_ENABLED: the PgBouncer listener port when pooling is on "
        "(the default), the direct Postgres port when off — so a normal process "
        "never needs to know the pooler exists. The admin plane (migrations / "
        "pg_dump / provisioning) derives the direct Postgres URL from the "
        "registry record instead of this field.",
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    redis_url: str = Field(
        alias="AVA_REDIS_URL",
        description="Redis connection URL. Carries the runtime ACL user's password "
        "as userinfo, so it is sensitive.",
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    redis_bin_dir: str = Field(
        default="",
        alias="AVA_REDIS_BIN_DIR",
        description="Absolute directory containing redis-server and redis-cli for this "
        "unit. Empty keeps the platform default. Read from this home's .env, never "
        "an inherited override. Both tools must be executable; takes effect on the "
        "next data-plane start, not by replacing an already-running Redis.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    @field_validator("redis_bin_dir")
    @classmethod
    def _absolute_redis_bin_dir(cls, value: str) -> str:
        if value and (not Path(value).is_absolute() or any(ord(c) < 32 for c in value)):
            raise ValueError(
                "redis_bin_dir must be an absolute directory without control characters"
            )
        return value

    pgbouncer_enabled: bool = Field(
        default=True,
        alias="AVA_PGBOUNCER_ENABLED",
        description="Whether this cluster's PgBouncer transaction pooler fronts "
        "Postgres. Decides the port AVA_DB_URL carries: the pooler listener when "
        "on (the default — the density path past ~50 agents, where raising "
        "Postgres max_connections hits per-connection memory overhead), the "
        "direct Postgres port when off. On by default; set false as a kill-switch "
        "(+ restart): converge rewrites AVA_DB_URL to the direct port and the "
        "pooler never starts. Normal processes see only AVA_DB_URL either way. "
        "The admin plane (migrations / pg_dump / provisioning) always bypasses "
        "the pooler, regardless of this toggle.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    data_plane_host: str = Field(
        default="",
        alias="AVA_DATA_PLANE_HOST",
        description=(
            "Host the per-cluster data-plane URLs are DERIVED with (install "
            "birth / registry-record carry). Empty = loopback (127.0.0.1) — the "
            "single-box posture every cluster uses today. Set it to a reachable "
            "address when the data plane is hosted off-box (external-migration "
            "step of Task #1752): the born AVA_DB_URL / AVA_REDIS_URL then carry "
            "that host, and the self-dial loopback rewrite (`_loopback_if_self`) "
            "passes a foreign host through untouched. The URLs, not this knob, "
            "are what every process dials — this field only decides the host "
            "they are born with; the registry record snapshots it at birth "
            "(`cli.commands.cluster_lifecycle._ensure_record`)."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    events_channel: str = Field(
        default="ava:events",
        alias="AVA_EVENTS_CHANNEL",
        description="Redis pub/sub channel name for cluster events.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    cluster_secret: str = Field(
        default="",
        alias="AVA_CLUSTER_SECRET",
        description=(
            "Single per-cluster pre-shared secret. EMPTY = a fully unauthenticated "
            "cluster: the gateway API and /ops serve without auth, Postgres/Redis "
            "run without scram/requirepass (loopback-trust only), and every surface "
            "binds loopback alone — the single-box no-secret posture. NON-EMPTY = "
            "the bearer authenticating cross-machine control surfaces (/ops dials and "
            "runner /api/bootstrap). Data-plane credentials are independent: the owner "
            "and Redis default-user passwords remain gateway-local, while runner "
            "credentials are projected inside their connection URLs. Set the secret on "
            "the gateway and hand it to each runner out-of-band as AVA_CLUSTER_SECRET "
            "for `ava enroll`."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    db_admin_password: str = Field(
        default="",
        alias="AVA_DB_ADMIN_PASSWORD",
        description=(
            "Password for the main Postgres owner role. Gateway-local only: it is "
            "never distributed by bootstrap or passed to agent processes."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": True,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )

    redis_admin_password: str = Field(
        default="",
        alias="AVA_REDIS_ADMIN_PASSWORD",
        description=(
            "Password for Redis's default administrative user and requirepass. "
            "Gateway-local only: it is never distributed by bootstrap or passed to "
            "agent processes."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": True,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )

    db_sslmode: str = Field(
        default="",
        alias="AVA_DB_SSLMODE",
        description=(
            "libpq sslmode for every Postgres dial through the sanctioned entry "
            "points (shared.db.connect / pool). Empty (the default) leaves TLS to "
            "the URL and libpq's own default ('prefer'): a local cluster keeps its "
            "current behavior, and a SaaS URL that already names sslmode (Neon / "
            "Supabase / Cloud SQL all carry it) is respected as-is — the URL is "
            "the switch, this field is the fallback. Set it (e.g. 'require' / "
            "'verify-full') to force TLS on a remote or SaaS data plane whose URL "
            "does not name a mode; 'disable' is the deliberate plaintext escape "
            "hatch for a trusted private network. Applied only when the URL does "
            "not already carry an sslmode query parameter."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    db_pool_min_size: int = Field(
        default=1,
        alias="AVA_DB_POOL_MIN_SIZE",
        description=(
            "Default min_size for shared.db.pool() when a caller does not pass an "
            "explicit size. A remote/SaaS data plane with tight connection limits "
            "(e.g. a managed Postgres that caps concurrent connections) tunes its "
            "pool footprint from config instead of code."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    db_pool_max_size: int = Field(
        default=2,
        alias="AVA_DB_POOL_MAX_SIZE",
        description=(
            "Default max_size for shared.db.pool() when a caller does not pass an "
            "explicit size. See AVA_DB_POOL_MIN_SIZE."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    transport_encryption: str = Field(
        default="",
        alias="AVA_TRANSPORT_ENCRYPTION",
        description=(
            "Declared transport-encryption mode for this cluster's network surface: "
            "tls (TLS terminates in front of the gateway and ops servers), mtls "
            "(mutual TLS), or overlay (an encrypted private overlay network carries "
            "the whole path). Empty is undeclared; a secret cluster serving off-box "
            "refuses to start until one is declared."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    trusted_cidrs: str = Field(
        default="",
        alias="AVA_TRUSTED_CIDRS",
        description=(
            "Comma-separated CIDR ranges, beyond loopback (always trusted), allowed "
            "to reach the data plane (Postgres / Redis). Empty = loopback only. Set "
            "it to the private-network range the agent-runners reach the gateway "
            "from; each range becomes a scram-sha-256 pg_hba host line. The data "
            "plane binds the gateway's own reachable address, not all interfaces."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    @field_validator("cluster_secret")
    @classmethod
    def _validate_cluster_secret(cls, v: str) -> str:
        """A non-empty cluster secret must be a URL-safe bearer token.

        It is sent in HTTP authorization headers and written to runner enrollment
        state. Restrict it to the RFC 3986 unreserved set so it is safe to carry
        through those URL-adjacent control-plane surfaces; data-plane passwords
        have independent generation and handling paths.
        """
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")
        if v and not set(v) <= allowed:
            raise ValueError(
                "AVA_CLUSTER_SECRET must be a URL-safe token (letters, digits, and "
                "'._~-') — it is used as a control-plane bearer"
            )
        return v

    @model_validator(mode="after")
    def _dial_self_host_via_loopback(self) -> DataPlaneSettings:
        """Self-dial goes loopback: when a data-plane URL's host IS this machine's
        own reachable address (`AVA_MACHINE_HOST`), the dial host is rewritten to
        `127.0.0.1`. Isomorphic with the uniform network posture — a single box is
        just the case where the reachable address is loopback — so dialing yourself
        never leaves the box: it must not route through the NIC or a VPN's network
        extension (a userspace VPN overlay can transiently black-hole a
        self-connect to its own IP: the TCP handshake completes but the forwarding
        leg is dead). The data plane always binds `127.0.0.1` first
        (`_bind_addrs`), so the loopback dial is always valid on the host that
        carries it. A URL naming another machine passes through untouched, so the
        bootstrap-served URL keeps working on remote runners.

        Only the in-memory dial value changes: the `.env` file, the verbatim
        bootstrap payload (`bootstrap_config_values` serves raw `.env` text), and
        the machine-registration address are all upstream of this rewrite.
        `AVA_DB_URL` is the one dial URL (pooler port when enabled), so the
        rewrite covers both pg and redis here.

        After an external data-plane migration (Task #1752) the URLs name a
        foreign host, which never equals `_self_machine_host()`, so no rewrite
        happens and every dial goes off-box as the URL states — the rewrite
        stays a self-dial optimization and is never extended to foreign hosts.
        """
        self.db_url = _loopback_if_self(self.db_url)
        self.redis_url = _loopback_if_self(self.redis_url)
        return self

    @model_validator(mode="after")
    def _apply_data_plane_passwords(self) -> DataPlaneSettings:
        """Re-apply the main Postgres owner password on every load,
        keeping the URL's username and database untouched.

        Names-as-data (path-only identity): the db/role/ACL identifier a cluster
        uses is whatever its `.env` URLs carry — an existing cluster on the
        historical `ava_main`, a fresh one on the fixed `ava` — and nothing
        re-derives it from a name. Only the owner password is re-derived, so an
        out-of-date DB URL self-heals after owner-password rotation while a
        data-plane rename stays a pure ops edit of the URLs. Redis is left
        verbatim because its URL carries the independent runtime ACL password.

        One identity is exempt: the least-privilege `ava_runner` db role (Task
        #1236). Its URL is projected by the gateway's /api/bootstrap?role=runner
        with the runner's OWN password (AVA_RUNNER_DB_PASSWORD), freshly read
        from the gateway .env at every fetch — never stale, and deliberately NOT
        the owner password. Overwriting it with the owner password would make
        every runner SASL-fail: the role's stored
        verifier is its own password, not the secret. The runner is identified by
        its URL username (names-as-data — the same read the rest of the system
        uses), so an ops rename of the main identity cannot mis-classify it.

        No-op when cluster_secret is empty (a no-auth cluster's URLs already carry
        the identity username with no password; tests / an unprovisioned checkout
        leave the URLs verbatim), for db_url when it is the unanchored sentinel
        (it carries no userinfo and must stay byte-identical for the connect
        guard), or when the URL names a FOREIGN host — a remote/SaaS plane's
        credential is the provider's, so the URL is authoritative and must never
        be replaced by the local owner password (Task #1752). The self-dial
        loopback rewrite runs before this validator, so a URL naming this
        machine's own reachable address is already loopback here and keeps the
        local password self-heal; only a genuinely foreign host is exempt.

        Runs after `_dial_self_host_via_loopback` (declared above it), so the
        local/foreign decision sees the dial host the cluster will actually use.
        """
        local_owner_url = (
            self.db_url != UNANCHORED_DB_SENTINEL
            and not _is_runner_db_url(self.db_url)
            and is_loopback_host(urlsplit(self.db_url).hostname or "")
        )
        home = _unit_home().expanduser().resolve()
        on_default_home = home == (Path.home() / ".ava").resolve()
        # AVA_PROCESS_PROFILE is a launcher-only process marker, not a Settings
        # field. It must remain an environment read so agent startup can preserve
        # its injected database projection.
        agent_profile_on_default_home = (
            os.environ.get("AVA_PROCESS_PROFILE") == "agent" and on_default_home
        )
        if agent_profile_on_default_home and self.cluster_secret and local_owner_url:
            # Agent-profile startup protects launcher-injected AVA_DB_URL from
            # dotenv replacement. Without this check, a missing projection would
            # combine the owner username with the cluster secret after agent
            # hygiene removes the owner password, granting the wrong identity.
            # Non-default homes are test/e2e clusters that deliberately retain
            # the owner URL topology to exercise password derivation.
            raise ValueError(
                "agent-profile processes must receive an ava_runner AVA_DB_URL; "
                "refusing a local owner URL derived from AVA_CLUSTER_SECRET "
                "at the default home"
            )
        if self.cluster_secret and local_owner_url:
            self.db_url = url_with_password(
                self.db_url, self.db_admin_password or self.cluster_secret
            )
        return self

    @model_validator(mode="after")
    def _pin_ipv4_hostaddr(self) -> DataPlaneSettings:
        """Append `hostaddr=<host>` to db_url when its host is an IPv4
        literal — libpq's own documented resolution bypass: `hostaddr` dials
        the given address directly and never resolves `host` at all when
        both are set (verified: connecting with a nonexistent `host` and a
        real `hostaddr` reaches the `hostaddr` server, no DNS error for
        `host`). `host` is left as-is (still used for SSL server-name
        checks and to label the connection in logs) — only the actual TCP
        dial is pinned.

        Defense-in-depth, not a fix for a proven psycopg bug: psycopg's own
        `_conninfo_attempts._resolve_hostnames` already checks
        `is_ip_address(host)` and skips `getaddrinfo` for a literal (mirrors
        asyncio's `_ensure_resolved` / anyio's `connect_tcp` — see
        shared/http_dial.py's module docstring for the same pattern in
        httpx), so psycopg itself is not vulnerable to the NAT64/DNS64
        synthesis failure mode `shared.netutil.is_ipv4_literal` documents.
        This still earns its keep for every OTHER libpq consumer that reads
        db_url — PgBouncer's own upstream dial to Postgres, `psql` /
        `pg_dump`, any future non-psycopg tool — which don't get psycopg's
        Python-level pre-check for free.

        Runs after `_dial_self_host_via_loopback` so hostaddr always mirrors
        whatever host that rewrite already settled on (loopback or a peer's
        address) rather than a pre-rewrite value. A hostname db_url (nothing
        to pin — normal resolution already reaches the right place) and the
        unanchored sentinel (must stay byte-identical for the connect guard)
        are untouched.
        """
        if self.db_url != UNANCHORED_DB_SENTINEL:
            host = urlsplit(self.db_url).hostname or ""
            if is_ipv4_literal(host):
                self.db_url = url_with_query_param(self.db_url, "hostaddr", host)
        return self

    @property
    def is_remote(self) -> bool:
        """Whether this cluster's data plane is hosted off-box (Task #1752).

        True when either URL's dial host — after the self-dial loopback rewrite
        (`_dial_self_host_via_loopback`) — is a foreign (non-loopback) host. The
        URL is the switch: a local self-built instance keeps loopback URLs (and a
        self-named host is rewritten to loopback), while an external host on the
        private network (the `data_plane_host` birth knob) or a SaaS provider's
        URL names another machine and passes through untouched. The unanchored
        boot sentinel and a host-less (unix-socket) URL read as local, so this
        predicate can never misfire on the pre-install or admin-socket paths.

        The management plane keys off this one fact: a remote data plane has no
        local instance to bring up / stop / repair, no local PgBouncer, and no
        local roles/ACL to provision — those operations degrade to a reachability
        probe or a clear skip instead of mis-managing a foreign service.

        A mixed plane (one URL loopback, one foreign) also reads as remote: the
        local instance is then only half the story, so local management is skipped
        wholesale rather than half-applied.
        """
        for url in (self.db_url, self.redis_url):
            host = url_host(url)
            if host and not is_loopback_host(host):
                return True
        return False
