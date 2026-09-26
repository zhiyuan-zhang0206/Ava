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

Dry-run is the default and changes nothing. `--execute` requires the home's
application root to be absent, no persistent terminals and no active release
operation. Each step records its intent before its effect in
`$AVA_HOME/db-authority/cutover.json` (0600); a re-run continues from that
record and is a verified no-op once complete. Ambiguous state (partial
credentials, a Redis password the home does not record, a journal that
contradicts the `.env` or the ledger) is refused, never repaired.

Run it from the checkout that owns the home, in a gateway context:

    ava stop --keep-infra
    .venv/bin/python scripts/cutover_db_authority.py --home <home>
    .venv/bin/python scripts/cutover_db_authority.py --home <home> --execute
    ava start

Networked homes (remote agent-runners) are not converted here: their runners
hold owner-era credentials and need the fleet cutover's per-unit delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

import redis
from dotenv import dotenv_values
from redis.backoff import NoBackoff
from redis.exceptions import AuthenticationError
from redis.retry import Retry

from shared.cluster import get_record, identity_from_url, record_redis_port
from shared.cluster.derive import REDIS_PASSWORD_ENV
from shared.cluster.registry import ClusterRecord
from shared.config import settings
from shared.envfile import upsert_env
from shared.paths import ava_home
from shared.private_storage import ensure_private_dir, write_private_bytes
from shared.url_secret import url_with_userinfo
from shared.verified_file import regular_bytes

_ADMIN_ENV = "AVA_REDIS_ADMIN_PASSWORD"
_URL_ENV = "AVA_REDIS_URL"
_STEP_STATES: dict[str, tuple[str, ...]] = {
    "redis": ("converting", "done"),
    "db": ("converting", "done"),
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


def read_journal(home: Path) -> dict[str, str]:
    """The recorded state of each cutover step; empty before the first run."""
    path = journal_path(home)
    try:
        data: object = json.loads(regular_bytes(path))
    except FileNotFoundError:
        return {}
    journal = cast("dict[str, object]", data) if isinstance(data, dict) else {}
    steps = journal.get("steps")
    if (
        set(journal) != {"version", "home", "steps"}
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


def _record(home: Path, step: str, state: str) -> None:
    steps = read_journal(home)
    steps[step] = state
    ensure_private_dir(journal_path(home).parent)
    body = {"version": 1, "home": str(home), "steps": steps}
    write_private_bytes(journal_path(home), (json.dumps(body, sort_keys=True) + "\n").encode())


@dataclass(frozen=True)
class RedisEnv:
    """The home's persisted Redis credentials, read from its `.env` file."""

    url: str
    admin: str
    runtime: str
    cluster_secret: str

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

    from cli.commands._maintenance_data_plane import _request_stop
    from cli.commands._maintenance_stop import remaining, wait_for_exit
    from shared.cluster import ownership
    from shared.native_process.ownership import capture_tree

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
    from cli.commands import _cluster_instance as instance
    from cli.commands._maintenance_stop import deadline_after

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
    data_dir = instance._redis_data_dir()
    if live == "unauthenticated":
        asyncio.run(_shutdown_unauthenticated(port, data_dir, deadline_after(_STOP_TIMEOUT_S)))
    if instance._start_redis(port, env.admin, env.runtime, env.cluster_secret, env.identity):
        raise RuntimeError("Redis did not start authenticated; re-run to continue")
    _verify(port, env, data_dir)
    _record(home, "redis", "done")
    return f"redis: {action} complete, unauthenticated connections refused ({observed})"


@dataclass(frozen=True)
class DbEnv:
    """The home's database endpoint and legacy credentials, read from `.env`."""

    url: str
    owner: str
    database: str
    legacy_keys: tuple[str, ...]
    cluster_secret: str

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
    from shared.cluster.authority import AuthorityRefusedError, load_ledger

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

    from cli.commands import _cluster_instance as instance
    from shared.cluster import record_postgres_port

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
    from shared.cluster.authority import GATEWAY_GROUP, RUNNER_GROUP

    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY 1",
        ([env.owner, GATEWAY_GROUP, RUNNER_GROUP],),
    ).fetchall()
    return tuple(row[0] for row in rows)


def _convert_db(home: Path, record: ClusterRecord, env: DbEnv) -> int:
    """Every effect of the db step, each idempotent; returns the active number."""
    from functools import partial

    from cli.commands import _cluster_instance as instance
    from cli.commands._data_plane import (
        READONLY_GRANTEES,
        _ensure_pooler,
        admin_session,
        prove_generation_logins,
    )
    from cli.commands._pgbouncer import stop_pgbouncer
    from cli.commands.migrations import cmd_migrations_apply
    from shared.cluster import authority, record_postgres_port
    from shared.envfile import remove_env

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
    from shared.cluster.authority import AuthorityRefusedError, active_generation

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


def admitted_record(home: Path) -> ClusterRecord:
    """`home`'s registry record, only when it is this checkout's quiescent local
    gateway home: no application root, terminals or active release operation."""
    from cli.commands._maintenance_stop import require_no_terminals
    from cli.commands._root_driver import _require_root_absent
    from shared.release_operation import require_configuration_write_authorized

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
    _require_root_absent()
    require_no_terminals()
    return record


def admitted_redis_port(home: Path) -> int:
    """`home`'s Redis port under the same admission as `admitted_record`."""
    return record_redis_port(admitted_record(home))


def _run(home: Path, *, execute: bool) -> None:
    record = admitted_record(home)
    print(convert_redis(home, record_redis_port(record), execute=execute))
    print(convert_db(home, record, execute=execute))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument("--home", required=True, help="the home to convert (explicit)")
    parser.add_argument("--execute", action="store_true", help="perform the cutover")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    try:
        if not args.execute:
            _run(home, execute=False)
            print("[dry-run] no changes made.")
            return 0
        from shared.home_lifecycle_locks import resource_lock
        from shared.platform import file_lock

        # The same home lock order as `ava start`: start intent, then resources.
        with (
            file_lock(home / "start-intent.lock", timeout_s=30),
            resource_lock(purpose="scripts.cutover_db_authority"),
        ):
            _run(home, execute=True)
    except CutoverRefusedError as exc:
        print(f"✗ cutover refused, nothing changed by the refused step: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"✗ cutover incomplete; fix the cause and re-run to continue: {exc}", file=sys.stderr)
        return 1
    print("✓ cutover complete; run `ava start` to resume this home.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
