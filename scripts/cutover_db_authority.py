#!/usr/bin/env python3
"""One-time cutover of an existing home to the always-authenticated data plane.

The internal data plane always authenticates, whatever the control-plane bearer
(decisions/2026-09-26-internal-data-plane-always-authenticated.md). A fresh
home is born that way. A home born earlier keeps its old posture, and ordinary
`ava start` refuses it instead of converting it; this script is the one explicit
conversion (decisions/2026-09-27-write-generation-rollout-choices.md). It never
runs implicitly and is deleted after the fleet cutover
(future/infra/unified-cluster-lifecycle.md).

Step `redis`: an unauthenticated home has empty `AVA_REDIS_ADMIN_PASSWORD` /
`AVA_REDIS_PASSWORD` and a Redis without `requirepass`. The step mints both
passwords into the home's `.env` (the runtime one also inside `AVA_REDIS_URL`),
restarts the owned Redis from a `redis.conf` carrying `requirepass`, re-affirms
the ACL user with its password, and proves that an unauthenticated connection
is refused. A home born authenticated is only verified. Redis credentials never
rotate per rollout.

Step `db`: a home without a database authority ledger has a LOGIN schema owner
(with `AVA_DB_ADMIN_PASSWORD` on a secret home), a LOGIN `ava_runner`
(`AVA_RUNNER_DB_PASSWORD`), trust `pg_hba` lines and a trust or owner-password
pooler. The step stops the owned pooler, rewrites the always-authenticated
`pg_hba` and proves the postmaster enforces it, applies pending migrations
(the group grants need this checkout's schema), demotes the owner and
`ava_runner` to NOLOGIN without passwords, creates the `ava_gateway` group and
both groups' grants, proves every legacy session closed, creates the ledger,
mints write generation 0, restarts the pooler serving exactly that pair, proves
both logins, activates the generation, checks the catalog invariant, and only
then rewrites `.env`: `AVA_DB_URL` becomes the credential-free endpoint and the
owner and runner passwords are removed. A home born with a ledger is only
verified. Any release request prepared before the cutover no longer matches its
configuration digest and must be prepared again.

Step `api`: runners now authenticate with their write generation's machine API
token, so on a networked home the human `AVA_CLUSTER_SECRET` they hold copies
of rotates once here (`scripts/rotate_cluster_secret`, journaled as
fingerprints), with the telemetry relay token derived from it. Every home first
pins its logical-backup passphrase to `$AVA_HOME/backups/logical-backup.passphrase`
(backup-critical): `sha256(secret)`, so earlier backups keep decrypting; a single
box keeps its secret, and an empty one pins a minted passphrase instead.

Step `remote-units` (networked homes only): every `machine_units` row other
than this gateway unit is a remote unit whose runners held the owner-era
`ava_runner` login (the `db` step fenced it) and copies of both Redis
passwords. The operator classifies each one explicitly: `--unit MACHINE:HOME`
(included: a sealed bundle for generation 0 — runner login, API admission,
enrollment secret — in `--bundle-dir`, its transport key printed once) or
`--exclude-unit MACHINE:HOME` (paused or offline: no bundle, it stays fenced
until a later issue-unit). Units of paused machines must be excluded; an
unclassified or unknown unit refuses. The step also rotates both Redis
passwords, each staged in `db-authority/redis-<admin|runtime>.pending` so a
crash resumes with the same value, applied live and persisted to `.env` (the
admin one also to `redis.conf`); runners fetch the new runtime URL from
bootstrap. A single box has no remote unit and the step is a no-op.

Dry-run is the default and changes nothing. `--execute` requires the home's
application root to be absent, no persistent terminals and no active release
operation. Each step records its intent before its effect in
`$AVA_HOME/db-authority/cutover.json` (0600); a re-run continues from that
record and is a verified no-op once complete. Ambiguous state (partial
credentials, a Redis password the home does not record, a journal that
contradicts the `.env` or the ledger) is refused, never repaired.

Run it from the checkout that owns the home, in a gateway context (an adopted
home is already stopped), then start the home as the script's last line names.
Commands and flags: conventions/data-plane-secret-split.md; each runner installs
its bundle at its held first start (conventions/cutover-home-adoption.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import stat
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

import redis
from dotenv import dotenv_values
from redis.backoff import NoBackoff
from redis.exceptions import AuthenticationError
from redis.retry import Retry

from base.cluster import get_record, identity_from_url, record_redis_port
from base.cluster.derive import REDIS_PASSWORD_ENV
from base.cluster.registry import ClusterRecord
from base.config import settings
from base.deploy.progress_timeout import UNIT_BUNDLE_TTL_S
from base.deploy.release.verified_file import regular_bytes
from base.host.env.dotenv_file import upsert_env
from base.host.net.url_secret import url_with_userinfo
from base.host.private_storage import ensure_private_dir, write_private_bytes
from base.paths import ava_home
from cli.cutover_hold import start_instruction

_ADMIN_ENV = "AVA_REDIS_ADMIN_PASSWORD"
_URL_ENV = "AVA_REDIS_URL"
_STEP_STATES: dict[str, tuple[str, ...]] = {
    "redis": ("converting", "done"),
    "db": ("converting", "done"),
    "api": ("rotating", "done"),
    "remote-units": ("issuing", "done"),
}
_DB_URL_ENV = "AVA_DB_URL"
_LEGACY_DB_KEYS = ("AVA_DB_ADMIN_PASSWORD", "AVA_RUNNER_DB_PASSWORD")
_STOP_TIMEOUT_S = 30.0

EnvPosture = Literal["unauthenticated", "credentialed"]
LivePosture = Literal["absent", "unauthenticated", "authenticated"]


class CutoverRefusedError(RuntimeError):
    """Ambiguous or unsupported state, detected before this run changed anything."""


def journal_path(home: Path) -> Path:
    return home / "db-authority" / "cutover.json"


def _journal(home: Path) -> dict[str, object]:
    try:
        data: object = json.loads(regular_bytes(journal_path(home)))
    except FileNotFoundError:
        return {}
    return cast("dict[str, object]", data) if isinstance(data, dict) else {"": None}


def read_journal(home: Path) -> dict[str, str]:
    """The recorded state of each cutover step; empty before the first run."""
    path, journal = journal_path(home), _journal(home)
    if not journal:
        return {}
    steps = journal.get("steps")
    if (
        set(journal) - {"api"} != {"version", "home", "steps"}
        or journal["version"] != 1
        or journal["home"] != str(home)
        or not isinstance(steps, dict)
    ):
        raise CutoverRefusedError(f"unrecognized cutover journal: {path}")
    recorded = cast("dict[str, str]", steps)
    for step, state in recorded.items():
        if state not in _STEP_STATES.get(step, ()):
            raise CutoverRefusedError(f"unrecognized cutover journal step {step}={state}: {path}")
    return recorded


def _record(home: Path, step: str, state: str, *, api: dict[str, Any] | None = None) -> None:
    steps = read_journal(home)
    steps[step] = state
    ensure_private_dir(journal_path(home).parent)
    body: dict[str, object] = {"version": 1, "home": str(home), "steps": steps}
    recorded = api if api is not None else _journal(home).get("api")
    if recorded is not None:
        body["api"] = recorded
    write_private_bytes(journal_path(home), (json.dumps(body, sort_keys=True) + "\n").encode())


@dataclass(frozen=True)
class RedisEnv:
    """The home's persisted Redis credentials, read from its `.env` file."""

    url: str = field(repr=False)
    admin: str = field(repr=False)
    runtime: str = field(repr=False)
    cluster_secret: str = field(repr=False)

    @classmethod
    def read(cls, home: Path) -> RedisEnv:
        values = dotenv_values(home / ".env")
        url = values.get(_URL_ENV)
        if not url:
            raise CutoverRefusedError(f"{home / '.env'} has no {_URL_ENV}")
        return cls(
            url=url,
            admin=values.get(_ADMIN_ENV) or "",
            runtime=values.get(REDIS_PASSWORD_ENV) or "",
            cluster_secret=values.get("AVA_CLUSTER_SECRET") or "",
        )

    @property
    def identity(self) -> str:
        try:
            return identity_from_url(self.url)
        except ValueError as exc:
            raise CutoverRefusedError(str(exc)) from None

    def posture(self) -> EnvPosture:
        url_password = unquote(urlsplit(self.url).password or "")
        if not (self.admin or self.runtime or url_password):
            return "unauthenticated"
        if self.admin and self.runtime and url_password == self.runtime:
            return "credentialed"
        raise CutoverRefusedError(
            f"ambiguous Redis credentials: {_ADMIN_ENV}, {REDIS_PASSWORD_ENV} and the "
            f"{_URL_ENV} password must be all empty or all present, the URL carrying "
            f"the runtime password"
        )


def _probe_client(
    port: int, username: str | None = None, password: str | None = None
) -> redis.Redis:
    # redis-py retries ConnectionError subclasses, AuthenticationError included;
    # a posture probe must observe a refusal once, not back off and retry it.
    return redis.Redis(
        host="127.0.0.1",
        port=port,
        username=username,
        password=password,
        socket_timeout=3,
        retry=Retry(NoBackoff(), 0),
    )


def live_posture(port: int) -> LivePosture:
    """How this home's Redis port answers an unauthenticated client."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
    except ConnectionRefusedError:
        return "absent"
    with _probe_client(port) as client:
        try:
            client.ping()
        except AuthenticationError:
            return "authenticated"
    return "unauthenticated"


def _authenticates(port: int, password: str, username: str | None = None) -> bool:
    with _probe_client(port, username, password) as client:
        try:
            return bool(client.ping())
        except AuthenticationError:
            return False


async def _shutdown_unauthenticated(port: int, data_dir: Path, deadline: float) -> None:
    """Save and stop this home's password-less Redis under native custody."""
    from redis.asyncio import Redis as AsyncRedis
    from redis.asyncio.retry import Retry as AsyncRetry

    from base.cluster import ownership
    from base.native_process.ownership import capture_tree
    from cli.commands.data_plane.maintenance_stop import _request_stop
    from cli.commands.lifecycle.service_stop import remaining, wait_for_exit

    client = AsyncRedis(
        host="127.0.0.1",
        port=port,
        decode_responses=True,
        single_connection_client=True,
        socket_connect_timeout=remaining(deadline),
        socket_timeout=remaining(deadline),
        retry=AsyncRetry(NoBackoff(), 0),
    )
    custody = ownership.RedisConnectionCustody()
    try:
        identity = await custody.capture(client, port=port, data_dir=data_dir, deadline=deadline)
        if identity is None:
            return
        tree = capture_tree(identity)
        await _request_stop("redis", identity, client, deadline, save=True)
        wait_for_exit(tree, deadline)
        ownership.require_listener(None, port, required=False)
    finally:
        if client.connection is not None:
            await client.connection.disconnect(nowait=True)
        await client.aclose()


def _decide(state: str | None, env: RedisEnv, port: int, live: LivePosture) -> str:
    """The step's action, or CutoverRefusedError; decided before any effect."""
    posture = env.posture()
    if state == "done":
        if posture != "credentialed":
            raise CutoverRefusedError(
                "the journal records Redis converted, but .env has no credentials"
            )
        action = "verify"
    elif posture == "unauthenticated":
        if live == "authenticated":
            raise CutoverRefusedError("Redis requires a password this home does not record")
        action = "mint"
    else:
        if state is None and live == "unauthenticated":
            raise CutoverRefusedError(
                ".env carries Redis credentials but Redis serves unauthenticated, and no "
                "cutover journal claims those credentials"
            )
        action = "adopt"
    if live == "authenticated" and not _authenticates(port, env.admin):
        raise CutoverRefusedError(f"Redis requires a password other than this home's {_ADMIN_ENV}")
    return action


def _mint(home: Path, env: RedisEnv) -> RedisEnv:
    runtime = secrets.token_urlsafe(32)
    upsert_env(
        home / ".env",
        {
            _ADMIN_ENV: secrets.token_urlsafe(32),
            REDIS_PASSWORD_ENV: runtime,
            _URL_ENV: url_with_userinfo(env.url, env.identity, runtime),
        },
        audit_site="cutover_db_authority",
    )
    return RedisEnv.read(home)


def _verify(port: int, env: RedisEnv, data_dir: Path) -> None:
    if live_posture(port) != "authenticated":
        raise RuntimeError("Redis still accepts unauthenticated connections")
    if not _authenticates(port, env.admin):
        raise RuntimeError(f"{_ADMIN_ENV} does not authenticate the Redis default user")
    if not _authenticates(port, env.runtime, env.identity):
        raise RuntimeError("the runtime ACL user does not authenticate with its password")
    conf = (data_dir / "redis.conf").read_text().splitlines()
    if not any(line.startswith("requirepass ") for line in conf):
        raise RuntimeError("redis.conf does not persist requirepass")


def convert_redis(home: Path, port: int, *, execute: bool) -> str:
    """Convert (or verify) this home's Redis; return a one-line outcome."""
    from cli.commands.data_plane import cluster_instance as instance
    from cli.commands.lifecycle.service_stop import deadline_after

    state = read_journal(home).get("redis")
    env = RedisEnv.read(home)
    live = live_posture(port)
    action = _decide(state, env, port, live)
    observed = f"journal={state or 'none'} env={env.posture()} redis={live}"
    if not execute:
        return f"redis: would {action} ({observed})"
    if action != "verify":
        _record(home, "redis", "converting")
    if action == "mint":
        env = _mint(home, env)
    data_dir = instance.redis_data_dir()
    if live == "unauthenticated":
        asyncio.run(_shutdown_unauthenticated(port, data_dir, deadline_after(_STOP_TIMEOUT_S)))
    if instance.start_redis(port, env.admin, env.runtime, env.cluster_secret, env.identity):
        raise RuntimeError("Redis did not start authenticated; re-run to continue")
    _verify(port, env, data_dir)
    _record(home, "redis", "done")
    return f"redis: {action} complete, unauthenticated connections refused ({observed})"


@dataclass(frozen=True)
class DbEnv:
    """The home's database endpoint and legacy credentials, read from `.env`."""

    url: str = field(repr=False)
    owner: str
    database: str
    legacy_keys: tuple[str, ...]
    cluster_secret: str = field(repr=False)

    @classmethod
    def read(cls, home: Path) -> DbEnv:
        values = dotenv_values(home / ".env")
        url = values.get(_DB_URL_ENV)
        if not url:
            raise CutoverRefusedError(f"{home / '.env'} has no {_DB_URL_ENV}")
        parts = urlsplit(url)
        owner, database = parts.username or "", parts.path.strip("/")
        if not owner or owner != database:
            raise CutoverRefusedError(
                f"{_DB_URL_ENV} must name the schema owner and its same-named database"
            )
        return cls(
            url=url,
            owner=owner,
            database=database,
            legacy_keys=tuple(key for key in _LEGACY_DB_KEYS if values.get(key) is not None),
            cluster_secret=values.get("AVA_CLUSTER_SECRET") or "",
        )

    @property
    def endpoint(self) -> str:
        """The credential-free endpoint `.env` carries after the cutover."""
        return url_with_userinfo(self.url, self.owner, "")

    @property
    def credential_free(self) -> bool:
        return not urlsplit(self.url).password and not self.legacy_keys


def _decide_db(state: str | None, env: DbEnv, home: Path) -> str:
    """The db step's action, or CutoverRefusedError; decided before any effect."""
    from base.cluster.authority import AuthorityRefusedError, load_ledger

    try:
        ledger = load_ledger(home)
    except AuthorityRefusedError as exc:
        raise CutoverRefusedError(f"the database authority store is not usable: {exc}") from exc
    if ledger is not None and ledger.owner != env.owner:
        raise CutoverRefusedError(
            f"the ledger records owner {ledger.owner!r}, but {_DB_URL_ENV} names {env.owner!r}"
        )
    if state == "done":
        if ledger is None or ledger.active is None or not env.credential_free:
            raise CutoverRefusedError(
                "the journal records the database converted, but the ledger has no active "
                "generation or .env still carries legacy credentials"
            )
        return "verify"
    if ledger is not None and state is None:
        if not env.credential_free:
            raise CutoverRefusedError(
                "a database authority ledger exists without a cutover journal, but .env still "
                "carries legacy credentials"
            )
        return "verify"
    return "convert"


def _refuse_superuser_owner(record: ClusterRecord, env: DbEnv) -> None:
    """A superuser owner (the initdb bootstrap superuser included) cannot become
    NOLOGIN; refuse before any effect when the postmaster is up to ask. A stopped
    postmaster defers the same refusal to `retire_legacy_logins`."""
    import psycopg

    from base.cluster import record_postgres_port
    from cli.commands.data_plane import cluster_instance as instance

    port = record_postgres_port(record)
    if not instance._pg_running(port):
        return
    with psycopg.connect(instance.pg_admin_url(port), autocommit=True) as conn:
        row = conn.execute(
            "SELECT rolsuper OR oid = 10 FROM pg_roles WHERE rolname = %s", (env.owner,)
        ).fetchone()
    if row is not None and row[0]:
        raise CutoverRefusedError(
            f"schema owner {env.owner!r} is a superuser and cannot become NOLOGIN; the owner "
            "must be a role distinct from the OS user"
        )


def _legacy_roles(conn: Any, env: DbEnv) -> tuple[str, ...]:
    from base.cluster.authority import GATEWAY_GROUP, RUNNER_GROUP

    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY 1",
        ([env.owner, GATEWAY_GROUP, RUNNER_GROUP],),
    ).fetchall()
    return tuple(row[0] for row in rows)


def _convert_db(home: Path, record: ClusterRecord, env: DbEnv) -> int:
    """Every effect of the db step, each idempotent; returns the active number."""
    from functools import partial

    from base.cluster import authority, record_postgres_port
    from base.host.env.dotenv_file import remove_env
    from cli.commands.data_plane import cluster_instance as instance
    from cli.commands.data_plane.bringup import (
        READONLY_GRANTEES,
        _ensure_pooler,
        admin_session,
        prove_generation_logins,
    )
    from cli.commands.data_plane.pgbouncer import stop_pgbouncer
    from cli.commands.lifecycle.migrations import cmd_migrations_apply

    groups = authority.Groups(gateway=authority.GATEWAY_GROUP, runner=authority.RUNNER_GROUP)
    cutover = authority.CutoverAuthority()
    # The pooler held the legacy front door; the application root is absent.
    stop_pgbouncer(force=True)
    if instance._start_pg(record_postgres_port(record), env.cluster_secret):
        raise RuntimeError("postgres did not start with the always-authenticated pg_hba")
    cmd_migrations_apply()
    with admin_session(record, env.database) as conn:
        authority.retire_legacy_logins(conn, owner=env.owner, groups=groups, authority=cutover)
        authority.ensure_groups(conn, owner=env.owner, database=env.database, groups=groups)
        authority.ensure_monitor(conn, database=env.database)
        authority.prove_closure(conn, _legacy_roles(conn, env))
        authority.create_ledger(home, owner=env.owner, groups=groups, authority=cutover)
        authority.ensure_pooler_admin(home, encrypt=partial(authority.scram_verifier, conn))
        authority.mint_generation(conn, home, cutover)
        generation = authority.require_ledger(home).unrevoked
        if generation is None:
            raise RuntimeError("the cutover minted no write generation")
        _ensure_pooler(record, env.database, home, generation)
        prove_generation_logins(home, generation, env.endpoint)
        authority.activate(home, cutover, authority.verify_generation(conn, home))
        authority.check_invariant(
            conn, home, database=env.database, readonly_grantees=READONLY_GRANTEES
        )
    upsert_env(home / ".env", {_DB_URL_ENV: env.endpoint}, audit_site="cutover_db_authority")
    remove_env(home / ".env", set(_LEGACY_DB_KEYS), audit_site="cutover_db_authority")
    return generation.number


def convert_db(home: Path, record: ClusterRecord, *, execute: bool) -> str:
    """Convert (or verify) this home's database authority; return a one-line outcome."""
    from base.cluster.authority import AuthorityRefusedError, active_generation

    state = read_journal(home).get("db")
    env = DbEnv.read(home)
    action = _decide_db(state, env, home)
    observed = (
        f"journal={state or 'none'} owner={env.owner} legacy_keys={list(env.legacy_keys) or 'none'}"
    )
    if not execute:
        return f"db: would {action} ({observed})"
    if action == "verify":
        try:
            active = active_generation(home)
        except AuthorityRefusedError as exc:
            raise CutoverRefusedError(str(exc)) from exc
        return f"db: verified, write generation {active.number} active ({observed})"
    _refuse_superuser_owner(record, env)
    _record(home, "db", "converting")
    number = _convert_db(home, record, env)
    _record(home, "db", "done")
    return (
        f"db: converted, write generation {number} active, owner NOLOGIN, .env holds the "
        f"credential-free endpoint ({observed})"
    )


def convert_api(home: Path, record: ClusterRecord, *, execute: bool) -> str:
    """Rotate the human bearer once on a networked home (step `api`)."""
    from base.cluster import record_postgres_port
    from cli.commands.data_plane import cluster_instance as instance
    from scripts.data_plane_ops import rotate_cluster_secret as bearer

    state, raw = read_journal(home).get("api"), _journal(home).get("api")
    rotation = None if raw is None else bearer.Rotation.parse(raw)
    if state is None:
        if not instance._pg_running(record_postgres_port(record)):
            if not execute:
                return "api: would decide once PostgreSQL runs"
            raise RuntimeError("the owned PostgreSQL is not running; `ava stop --keep-infra`")
        if not _remote_inventory(record, DbEnv.read(home).database, home)[0]:
            return bearer.pin_single_box(home, execute=execute)
    if not execute:
        verb = "verify" if state == "done" else "pin the logical-backup passphrase and rotate"
        return f"api: would {verb} AVA_CLUSTER_SECRET (journal={state or 'none'})"
    if state is None:
        _record(home, "api", "rotating")
    done = bearer.advance(
        home, rotation, lambda current: _record(home, "api", "rotating", api=asdict(current))
    )
    _record(home, "api", "done", api=asdict(done))
    return (
        "api: AVA_CLUSTER_SECRET rotated once (runners' copies evicted); the logical-backup "
        f"passphrase is pinned at {home / 'backups' / 'logical-backup.passphrase'} — "
        "backup-critical: keep it with the gateway's backup keys"
    )


Units = set[tuple[str, str]]


@dataclass(frozen=True)
class UnitPlan:
    """The operator's explicit classification of the remote units."""

    include: tuple[tuple[str, str], ...] = ()
    exclude: tuple[tuple[str, str], ...] = ()
    bundle_dir: Path | None = None

    @staticmethod
    def parse(value: str) -> tuple[str, str]:
        machine, sep, unit_home = value.partition(":")
        if not (sep and machine and unit_home):
            raise argparse.ArgumentTypeError(f"{value!r} is not MACHINE:HOME")
        return machine, unit_home


def _remote_inventory(record: ClusterRecord, database: str, home: Path) -> tuple[Units, Units]:
    """(remote units, units of paused machines) from the gateway's own tables."""
    from base.cluster.machine import machine_name
    from cli.commands.data_plane.bringup import admin_session

    with admin_session(record, database) as conn:
        units = {(m, h) for m, h in conn.execute("SELECT machine_name, home FROM machine_units")}
        paused = {
            (m, h)
            for m, h in conn.execute(
                "SELECT u.machine_name, u.home FROM machine_units u"
                " JOIN machines m ON m.name = u.machine_name WHERE m.paused_at IS NOT NULL"
            )
        }
    units.discard((machine_name(), str(home)))
    return units, paused


def _require_classified(plan: UnitPlan, remote: Units, paused: Units) -> None:
    include, exclude = set(plan.include), set(plan.exclude)
    if include & exclude:
        raise CutoverRefusedError(f"units both included and excluded: {sorted(include & exclude)}")
    if include | exclude != remote:
        raise CutoverRefusedError(
            "classify every remote unit exactly once with --unit / --exclude-unit: "
            f"unclassified {sorted(remote - include - exclude)}, "
            f"unknown {sorted((include | exclude) - remote)}"
        )
    if include & paused:
        raise CutoverRefusedError(
            f"paused machines' units must be excluded: {sorted(include & paused)}"
        )
    if include and plan.bundle_dir is None:
        raise CutoverRefusedError("included units need --bundle-dir for their bundles")


def _pending(home: Path, name: str) -> str:
    """The staged next Redis `name` password, created once (0600)."""
    path = home / "db-authority" / f"redis-{name}.pending"
    if not path.exists():
        write_private_bytes(path, (secrets.token_urlsafe(32) + "\n").encode())
    return regular_bytes(path).decode().strip()


def _rotate_redis(home: Path, port: int) -> None:
    """Replace both Redis passwords: runner homes, their residue and the W3 copies
    hold the admin one and the runtime one (bootstrap served it to every runner)."""
    from base.cluster import ensure_cluster_redis_acl
    from cli.commands.data_plane import cluster_instance as instance

    env = RedisEnv.read(home)
    admin, runtime = _pending(home, "admin"), _pending(home, "runtime")
    if not _authenticates(port, admin):
        if not _authenticates(port, env.admin):
            raise CutoverRefusedError(
                "Redis accepts neither this home's admin password nor the staged one"
            )
        with _probe_client(port, password=env.admin) as client:
            client.config_set("requirepass", admin)
    instance._write_redis_conf(instance.redis_data_dir(), admin)
    ensure_cluster_redis_acl(
        env.identity,
        redis_admin_url=f"redis://default:{admin}@127.0.0.1:{port}",
        runtime_password=runtime,
        channel_prefix=settings.data_plane.events_channel.removesuffix(":events"),
        expected_data_dir=instance.redis_data_dir(),
    )
    url = url_with_userinfo(env.url, env.identity, runtime)
    upsert_env(
        home / ".env",
        {_ADMIN_ENV: admin, REDIS_PASSWORD_ENV: runtime, _URL_ENV: url},
        audit_site="cutover_db_authority",
    )
    for old, new, user in ((env.admin, admin, None), (env.runtime, runtime, env.identity)):
        if old != new and _authenticates(port, old, user):
            raise RuntimeError(f"Redis accepts the replaced password of user {user or 'default'}")
    _verify(port, RedisEnv.read(home), instance.redis_data_dir())
    for name in ("admin", "runtime"):
        (home / "db-authority" / f"redis-{name}.pending").unlink()


def _issue_bundles(home: Path, plan: UnitPlan, bundle_dir: Path) -> list[str]:
    from base.cluster.authority.unit import UnitIdentity, issue_bundle, write_bundle
    from base.config.service_read import served_db_endpoint

    bundle_dir.mkdir(mode=0o700, exist_ok=True)
    info = bundle_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise CutoverRefusedError(f"--bundle-dir {bundle_dir} must be an owner-only directory")
    lines: list[str] = []
    # Fresh from `.env`: the `api` step may have rotated the secret in this run.
    cluster_secret = dotenv_values(home / ".env").get("AVA_CLUSTER_SECRET") or ""
    for machine, unit_home in plan.include:
        unit = UnitIdentity(machine=machine, home=unit_home)
        issued = issue_bundle(
            home,
            unit=unit,
            endpoint=served_db_endpoint(),
            cluster_secret=cluster_secret,
            ttl_s=UNIT_BUNDLE_TTL_S,
        )
        target = bundle_dir / f"{machine}-{unit.key[:12]}.bundle"
        target.unlink(missing_ok=True)
        write_bundle(target, issued.envelope)
        lines.append(f"  {unit.describe()}: {target} transport key {issued.transport_key}")
    return lines


def convert_remote_units(
    home: Path, record: ClusterRecord, plan: UnitPlan, *, execute: bool
) -> str:
    """Classify the remote units, rotate both Redis passwords and issue bundles."""
    from base.cluster import record_postgres_port
    from cli.commands.data_plane import cluster_instance as instance

    state = read_journal(home).get("remote-units")
    env = DbEnv.read(home)
    if not instance._pg_running(record_postgres_port(record)):
        if not execute:
            return "remote-units: would classify machine_units once PostgreSQL runs"
        raise RuntimeError(
            "the owned PostgreSQL is not running; stop the home with `ava stop --keep-infra`"
        )
    remote, paused = _remote_inventory(record, env.database, home)
    if not remote and not (plan.include or plan.exclude):
        return "remote-units: none (single box)"
    _require_classified(plan, remote, paused)
    observed = f"journal={state or 'none'} include={len(plan.include)} exclude={len(plan.exclude)}"
    if state == "done":
        return f"remote-units: verified, bundles were issued ({observed})"
    if not execute:
        return f"remote-units: would rotate both Redis passwords and issue bundles ({observed})"
    _record(home, "remote-units", "issuing")
    _rotate_redis(home, record_redis_port(record))
    lines = [] if plan.bundle_dir is None else _issue_bundles(home, plan, plan.bundle_dir)
    _record(home, "remote-units", "done")
    excluded = ", ".join(f"{m}:{h}" for m, h in plan.exclude) or "none"
    head = f"remote-units: Redis passwords rotated; excluded units stay fenced ({excluded}); "
    return "\n".join([head + "bundles (carry each with its key; shown once):", *lines])


def admitted_record(home: Path) -> ClusterRecord:
    """`home`'s registry record, only when it is this checkout's quiescent local
    gateway home: no application root, terminals or active release operation."""
    from base.deploy.release.operation import require_configuration_write_authorized
    from cli.commands.lifecycle.root_driver import require_root_absent
    from cli.commands.lifecycle.service_stop import require_no_terminals

    if home != ava_home().resolve():
        raise CutoverRefusedError(
            f"--home {home} is not this checkout's home ({ava_home()}); run the script "
            "with the .venv of the checkout that owns the home"
        )
    if settings.data_plane.is_remote:
        raise CutoverRefusedError("the data plane is remote-managed; the provider owns its auth")
    record = get_record(home)
    if record is None:
        raise CutoverRefusedError(f"{home} has no gateway registry record")
    require_configuration_write_authorized(home)
    require_root_absent()
    require_no_terminals()
    return record


def _run(home: Path, *, execute: bool, plan: UnitPlan) -> None:
    record = admitted_record(home)
    print(convert_redis(home, record_redis_port(record), execute=execute))
    print(convert_db(home, record, execute=execute))
    print(convert_api(home, record, execute=execute))
    print(convert_remote_units(home, record, plan, execute=execute))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument("--home", required=True, help="the home to convert (explicit)")
    parser.add_argument("--execute", action="store_true", help="perform the cutover")
    parser.add_argument(
        "--unit", action="append", default=[], type=UnitPlan.parse, metavar="MACHINE:HOME"
    )
    parser.add_argument(
        "--exclude-unit", action="append", default=[], type=UnitPlan.parse, metavar="MACHINE:HOME"
    )
    parser.add_argument("--bundle-dir", default=None, help="private directory for the bundles")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    plan = UnitPlan(
        include=tuple(args.unit),
        exclude=tuple(args.exclude_unit),
        bundle_dir=None
        if args.bundle_dir is None
        else Path(args.bundle_dir).expanduser().absolute(),
    )
    try:
        if not args.execute:
            _run(home, execute=False, plan=plan)
            print("[dry-run] no changes made.")
            return 0
        from base.deploy.lifecycle.home_lifecycle_locks import resource_lock
        from base.native_process.os_platform import file_lock

        # The same home lock order as `ava start`: start intent, then resources.
        with (
            file_lock(home / "start-intent.lock", timeout_s=30),
            resource_lock(purpose="scripts.cutover_db_authority"),
        ):
            _run(home, execute=True, plan=plan)
    except CutoverRefusedError as exc:
        print(f"✗ cutover refused, nothing changed by the refused step: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"✗ cutover incomplete; fix the cause and re-run to continue: {exc}", file=sys.stderr)
        return 1
    print(f"✓ cutover complete; run {start_instruction(home)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
