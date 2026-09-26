#!/usr/bin/env python3
"""One-time cutover of an existing home to the always-authenticated data plane.

The internal data plane always authenticates, whatever the control-plane bearer
(decisions/2026-09-26-internal-data-plane-always-authenticated.md). A fresh
home is born that way. A home born earlier keeps its old posture, and ordinary
`ava start` refuses it instead of converting it; this script is the one explicit
conversion. It never runs implicitly and is deleted after the fleet cutover
(future/infra/unified-cluster-lifecycle.md).

Step `redis`: an unauthenticated home has empty `AVA_REDIS_ADMIN_PASSWORD` /
`AVA_REDIS_PASSWORD` and a Redis without `requirepass`. The step mints both
passwords into the home's `.env` (the runtime one also inside `AVA_REDIS_URL`),
restarts the owned Redis from a `redis.conf` carrying `requirepass`, re-affirms
the ACL user with its password, and proves that an unauthenticated connection
is refused. A home born authenticated is only verified. Redis credentials never
rotate per rollout.

Dry-run is the default and changes nothing. `--execute` requires the home's
application root to be absent, no persistent terminals and no active release
operation. Each step records its intent before its effect in
`$AVA_HOME/db-authority/cutover.json` (0600); a re-run continues from that
record and is a verified no-op once complete. Ambiguous state (partial
credentials, a Redis password the home does not record, a journal that
contradicts the `.env`) is refused, never repaired.

Run it from the checkout that owns the home, in a gateway context:

    ava stop --keep-infra
    .venv/bin/python scripts/cutover_db_authority.py --home <home>
    .venv/bin/python scripts/cutover_db_authority.py --home <home> --execute
    ava start
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
from typing import Literal, cast
from urllib.parse import unquote, urlsplit

import redis
from dotenv import dotenv_values
from redis.backoff import NoBackoff
from redis.exceptions import AuthenticationError
from redis.retry import Retry

from shared.cluster import get_record, identity_from_url, record_redis_port
from shared.cluster.derive import REDIS_PASSWORD_ENV
from shared.config import settings
from shared.envfile import upsert_env
from shared.paths import ava_home
from shared.private_storage import ensure_private_dir, write_private_bytes
from shared.url_secret import url_with_userinfo
from shared.verified_file import regular_bytes

_ADMIN_ENV = "AVA_REDIS_ADMIN_PASSWORD"
_URL_ENV = "AVA_REDIS_URL"
_STEP_STATES: dict[str, tuple[str, ...]] = {"redis": ("converting", "done")}
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


def admitted_redis_port(home: Path) -> int:
    """`home`'s Redis port, only when it is this checkout's quiescent local
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
    return record_redis_port(record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--home", required=True, help="the home to convert (explicit)")
    parser.add_argument("--execute", action="store_true", help="perform the cutover")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    try:
        port = admitted_redis_port(home)
        if not args.execute:
            print(convert_redis(home, port, execute=False))
            print("[dry-run] no changes made.")
            return 0
        from shared.home_lifecycle_locks import resource_lock
        from shared.platform import file_lock

        # The same home lock order as `ava start`: start intent, then resources.
        with (
            file_lock(home / "start-intent.lock", timeout_s=30),
            resource_lock(purpose="scripts.cutover_db_authority"),
        ):
            print(convert_redis(home, admitted_redis_port(home), execute=True))
    except CutoverRefusedError as exc:
        print(f"✗ cutover refused, nothing changed: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"✗ cutover incomplete; fix the cause and re-run to continue: {exc}", file=sys.stderr)
        return 1
    print("✓ cutover complete; run `ava start` to resume this home.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
