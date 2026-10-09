"""Fetch cluster-common config from the gateway at process start.

Every process on a configured pure agent-runner (serve_agent_runner flag on,
enrolled with a gateway URL) fetches the cluster's config from the gateway at
startup and injects it into os.environ before Settings is built — there is no
materialized `.env` cache of cluster facts anymore (retired 2026-08-01). The
one cache that does exist is a transient 0600 config snapshot under
`$AVA_HOME/run/`: a successful fetch writes it, and a later process skips the
fetch while it is fresh, falling back to it (with a warning) when the gateway
is unreachable — see the snapshot helpers below. A
gateway-capable unit keeps the cluster's config in its own `$AVA_HOME/.env` and
never fetches; which side a unit is on is derived from its serve flags (see
`config_source_is_local` / `should_fetch_from_gateway`), not from an env var
(AVA_CONFIG_SOURCE deleted). A bare checkout with no role flags (CI, lint
scripts) and a not-yet-enrolled runner resolve locally with no fetch — the preflight gate refuses an unenrolled
`ava start`, not the Settings import. The
bytes travel the private network; an authenticated gateway requires a bearer,
which this presents from the unit's machine API token (`AVA_API_TOKEN`, from
its installed capability); a unit never holds the human cluster secret.
Intentionally imports nothing from base.config (it runs DURING base.config
import) — only stdlib + base.host.net.predicates at import; base.host.env.dotenv_boot loads at
the fetch decision and the httpx / base.cluster.auth / base.host.net.http_dial
pieces lazily at fetch time (all config-free, so they're safe this early in
boot).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, cast

# An agent boots by fetching this. The timeout must cover a slow-but-healthy
# fetch under load (the whole boot -- fetch + import + claim -- has to finish
# before the parent's launch-confirm poll gives up; too small a cap fails a
# fetch that would have succeeded -- a 3s cap regressed CI). The retry covers a
# gateway briefly unreachable mid-restart (connection refused), which fails fast.
# A ReadTimeout (slow response) is deliberately NOT retried: stacking another
# full timeout would blow past the launch-confirm window and fail a spawn whose
# first fetch would have eventually returned.
_FETCH_ATTEMPTS = 2
_FETCH_TIMEOUT_S = 10.0


def service_plugin_config_packet() -> str | None:
    """The launcher's fixed plugin config snapshot for this service generation.

    Separate from cluster bootstrap: a later bootstrap fetch must not replace
    the values captured by this unit's manifest and gate.
    """
    from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV

    return os.environ.get(SERVICE_PLUGIN_CONFIG_ENV)


def cluster_plugin_config_values(plugin: str) -> dict[str, object] | None:
    """One plugin's cluster projection from the existing bootstrap payload.

    This carrier never contains host fields or service birth snapshots. Schema
    admission belongs to the declared plugin class at its composition boundary.
    """
    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV

    packet = os.environ.get(PLUGIN_CLUSTER_CONFIG_ENV)
    if packet is None:
        return None
    raw: object = json.loads(packet)
    if not isinstance(raw, dict):
        raise TypeError("plugin cluster config must be a JSON object")
    contents = cast("dict[str, object]", raw)
    if plugin not in contents:
        return None
    values = contents[plugin]
    if not isinstance(values, dict):
        raise TypeError(f"plugin cluster config {plugin!r} must be a JSON object")
    return cast("dict[str, object]", values)


def legacy_plugin_config_values(aliases: tuple[str, ...]) -> dict[str, str]:
    """Capture declared legacy inputs for one-time import; never write a home."""
    if not aliases:
        return {}
    from dotenv import dotenv_values

    from base.host.env.dotenv_boot import resolve_ava_home

    disk = dotenv_values(resolve_ava_home() / ".env")
    values: dict[str, str] = {}
    for alias in aliases:
        disk_value = disk.get(alias)
        delivered = os.environ.get(alias)
        if disk_value is not None and delivered is not None and disk_value != delivered:
            raise ValueError(f"legacy plugin input {alias} differs between environment and .env")
        value = delivered if delivered is not None else disk_value
        if value is not None:
            values[alias] = value
    return values


def consume_legacy_plugin_env(values: dict[str, str]) -> None:
    """Drop only process inputs successfully imported by the image writer."""
    for alias, captured in values.items():
        current = os.environ.get(alias)
        if current is not None and current != captured:
            raise RuntimeError(f"legacy plugin input {alias} changed during import")
        os.environ.pop(alias, None)


def _fetch_backoff(attempt: int) -> float:
    return 0.5 * (attempt + 1)


# The parent config snapshot (P0 #2100): a successful fetch writes the payload
# to `$AVA_HOME/run/bootstrap-snapshot.json` (0600). A later process on the same
# unit — most importantly the exec child the agent process spawns per turn —
# SKIPS the fetch while the snapshot is fresh, so a gateway that is down or
# restarting is no longer a single point that silently kills every execute_code
# call. When the snapshot is stale the process fetches as before; if that fetch
# then fails on a transport error the process falls back to the snapshot anyway
# (last-known cluster config — the same values its parent already runs on),
# because during a gateway outage no cluster config edit can have happened
# since the snapshot was written.
_SNAPSHOT_NAME = "bootstrap-snapshot.json"
# Version 3 snapshots carry no database credential (bootstrap serves the
# credential-free endpoint); an older snapshot may hold a runner login and is
# never reused.
_SNAPSHOT_VERSION = 3
# Freshness window: how long a snapshot may stand in for a live fetch. Bounds
# cluster-edit propagation to a few minutes in steady state (the first child
# past the window re-fetches and refreshes the snapshot) while keeping the
# common per-turn child boot fetch-free.
_SNAPSHOT_FRESH_S = 300.0


# Transport-level httpx failures mean "gateway unreachable / mid-restart" —
# the class that may fall back to a stale snapshot. Auth and status failures
# (401 wrong secret, 400, 5xx) still fail loud: a runner the gateway rejects
# must not keep running on old config.
def _transport_failures() -> tuple[type[BaseException], ...]:
    """Transport-level httpx failure classes (lazy import: httpx stays off the
    settings-import path until an actual fetch is attempted)."""
    import httpx

    return (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.PoolTimeout,
    )


# The maintenance-verb opt-out (settings-lite). `ava stop` / `ava status` / the
# watchdog probe / the thin-client verbs must still construct Settings while the
# gateway is down, so `cli.main` sets AVA_CONFIG_FETCH=skip for them before any
# settings-loading import. Every other process — start, converge, update, the
# daemons, the agents — derives its fetch decision purely from the role flag and
# fetches when it is a pure agent-runner. base.sessions.env_forwarding deliberately does NOT
# forward this var to spawned processes (a daemon/agent must fetch per its own
# role, never inherit a CLI verb's opt-out).
CONFIG_FETCH_ENV = "AVA_CONFIG_FETCH"
CONFIG_FETCH_SKIP = "skip"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _serve_flag(env_key: str) -> bool:
    """Settings-free mirror of one capability flag's resolution: env
    `AVA_MACHINE_SERVE_*` > `$AVA_HOME/.env` > False.

    Normal config boot has already loaded the `.env` before this function runs,
    so its value is in the environment. Settings-lite maintenance commands do
    not load it: read the file directly so `ava config --local` correctly sees
    a gateway unit without constructing Settings. `base.cluster.machine` reads the
    flag through Settings, which does not exist yet while this module runs. A
    malformed value resolves False here; the real resolver raises on it loudly
    later, so the config-source decision can never silently disagree with the
    machine role.

    The `.env` read resolves `$AVA_HOME` through `dotenv_boot.resolve_ava_home()`: at
    this point in boot the env var may not be pinned yet (see above).
    """
    raw = os.environ.get(env_key)
    if raw is not None and raw.strip():
        return raw.strip().lower() in _TRUTHY
    from base.host.env import runtime_config

    raw = runtime_config.read_env_aliases().get(env_key)
    if raw is not None and raw.strip():
        return raw.strip().lower() in _TRUTHY
    return False


def config_source_is_local() -> bool:
    """Whether this unit's config source is its own `$AVA_HOME/.env`.

    Role-derived (AVA_CONFIG_SOURCE deleted 2026-08-01): a unit that serves the
    gateway owns the cluster's config in its own `.env` and never fetches; a
    configured pure agent-runner holds only the bootstrap env (gateway URL +
    secret, from `ava start`) and fetches the rest from the gateway at every
    process start (see `should_fetch_from_gateway`). A bare checkout with no
    role flags — CI, lint scripts, dev tools — is not a unit yet and resolves
    locally with no fetch.

    The serve-gateway flag is read settings-free (env > `$AVA_HOME/.env` > False)
    because this runs DURING the Settings import, before base.cluster.machine can
    resolve the role. `cli.main` opts the maintenance verbs out of the fetch with
    AVA_CONFIG_FETCH=skip (see CONFIG_FETCH_ENV); that is orthogonal to this
    derivation.
    """
    return _serve_flag("AVA_MACHINE_SERVE_GATEWAY")


def should_fetch_from_gateway() -> bool:
    """Whether this process's Settings build fetches cluster config from the gateway.

    True only for a CONFIGURED pure agent-runner: the serve_agent_runner flag is
    on (env `AVA_MACHINE_SERVE_AGENT_RUNNER` > `$AVA_HOME/.env` > False) AND a gateway
    URL is present (`ava start` validates the remote projection before persisting
    runner identity). Everything else resolves locally:

    - a gateway-capable unit (config_source_is_local) never fetches;
    - a bare checkout with no role flags (CI, lint scripts, dev tools) and a
      not-yet-enrolled runner (flag on, no URL) construct Settings from their
      local env/.env with no fetch and no error — `ava start`'s preflight gate
      is what refuses an unenrolled runner, not the Settings import, so any
      tool that imports base.config keeps working on any machine.

    Reads os.environ only: by the time base.config calls this, load_ava_env
    has loaded (and `_enforce_cluster_env_authority` forced) the unit's .env,
    so the flag and the URL are both present in the environment when they
    exist on disk.
    """
    return _serve_flag("AVA_MACHINE_SERVE_AGENT_RUNNER") and bool(os.environ.get("AVA_GATEWAY_URL"))


class BootstrapFetchError(RuntimeError):
    """A pure agent-runner could not fetch its cluster config from the gateway:
    no gateway URL to fetch from, or every fetch attempt failed (unreachable /
    401 / bad body).

    The process must not start with no config. `ava start` exits 1 with this
    message (the boot policy retries it); a daemon dies at import and the OS
    watchdog probe revives it until the gateway is reachable again — the
    fetch's own retry plus that revive loop is what makes a gateway restart
    self-heal on a runner.
    """


def _validated_bootstrap_payload(payload: object) -> dict[str, str]:
    """Assert the bootstrap body is a flat ``{str: str}`` map before the caller
    writes it into ``os.environ``.

    A gateway version skew, a wrong endpoint, or a proxy substituting the body
    could feed a non-str value that either raises an opaque ``TypeError`` deep in
    ``os.environ.__setitem__`` or (for a str-able scalar) writes a value the
    recipient never round-trips. Fail loud at the trust boundary instead.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"/api/bootstrap returned {type(payload).__name__}, expected a JSON object")
    raw = cast("dict[str, Any]", payload)
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(
                "/api/bootstrap must be a flat {str: str} map; got "
                f"{type(key).__name__} key -> {type(value).__name__} value for {key!r}"
            )
    return raw


def dial_get(*args: Any, **kwargs: Any) -> Any:
    """Thin seam over `base.host.net.http_dial.get` (lazy: http_dial/httpx stay off the
    settings-import path; callers and tests patch this module attribute)."""
    from base.host.net.http_dial import get as _get

    return _get(*args, **kwargs)


def fetch_bootstrap_config(
    base_url: str,
    timeout: float = _FETCH_TIMEOUT_S,
    attempts: int = _FETCH_ATTEMPTS,
    *,
    bearer: str | None = None,
) -> dict[str, str]:
    """GET {base_url}/api/bootstrap. Return {alias: value}.

    Retries with linear backoff so a gateway that is briefly unreachable as the
    agent boots doesn't strand it.

    A runner never takes a database credential from bootstrap: any password in
    the served `AVA_DB_URL` (an older gateway still projecting a runner login) is
    removed here, before the value reaches the snapshot or `os.environ`. The
    runner's login comes only from its installed unit capability
    (`base.cluster.authority.unit`). Nor does it take the human cluster secret:
    an `AVA_CLUSTER_SECRET` in the payload is dropped the same way.

    Presents `Authorization: Bearer <bearer>`: an explicit `bearer` (a joining
    start holding its bundle's token), else this process's machine API token
    (`AVA_API_TOKEN`, delivered by the launcher or the boot pass), else none (an
    open cluster). Read from os.environ, not Settings — this runs during the
    Settings import.

    Raises:
        httpx.HTTPError: every attempt failed (gateway unreachable / non-2xx, e.g.
            401 when the machine token is missing, revoked or wrong).
        TypeError: the response body is not a flat ``{str: str}`` map.
    """
    import httpx

    from base.cluster.auth import bearer_header, delivered_token
    from base.host.net.resilience import Policy, retry

    token = bearer if bearer is not None else delivered_token()
    headers = bearer_header(token) if token else {}
    url = f"{base_url.rstrip('/')}/api/bootstrap"
    # Gateway briefly down: retry connect errors only; ReadTimeout propagates.
    # Keep the original 0.5s, 1.0s, ... schedule without Retry-After or jitter.
    policy = Policy(
        max_attempts=attempts,
        backoff=_fetch_backoff,
        jitter="none",
        jitter_span=1.0,
        classify=lambda exc: isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)),
        idempotent=True,
        respect_retry_after=False,
        on_final_failure=None,
    )

    def _fetch_once() -> dict[str, str]:
        resp = dial_get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
        values = _validated_bootstrap_payload(resp.json())
        if "AVA_DB_URL" in values:
            from base.cluster.authority.unit import credential_free

            values["AVA_DB_URL"] = credential_free(values["AVA_DB_URL"])
        # A unit never holds the human bearer, whatever a gateway serves.
        values.pop("AVA_CLUSTER_SECRET", None)
        return values

    return retry(policy)(_fetch_once)


def _snapshot_path() -> Path:
    """The unit's config snapshot path, under this process's home."""
    from base.host.env.dotenv_boot import resolve_ava_home

    return resolve_ava_home() / "run" / _SNAPSHOT_NAME


def _read_config_snapshot(base_url: str) -> tuple[dict[str, str], float] | None:
    """The last-known cluster config and its age in seconds, or None.

    Guards: the snapshot must have been written for the SAME gateway URL (a
    re-enrolled runner must not reuse another gateway's values), carry the
    current version, and hold a flat ``{str: str}`` map. Absent / unreadable /
    malformed reads as None — the caller falls through to the fetch."""
    path = _snapshot_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, Any]", raw)
    if data.get("v") != _SNAPSHOT_VERSION:
        return None
    if data.get("base_url") != base_url:
        return None
    written_at = data.get("written_at")
    if not isinstance(written_at, (int, float)):
        return None
    try:
        values = _validated_bootstrap_payload(data.get("values"))
    except TypeError:
        return None
    return values, max(0.0, time.time() - float(written_at))


def _write_config_snapshot(base_url: str, values: dict[str, str]) -> None:
    """Best-effort owner-only snapshot write (atomic replace).

    The snapshot carries cluster credentials, so it is 0600 like the rest of
    the unit's private storage. It never raises and never fails the boot: it
    is a cache, and the worst case of losing it is one extra fetch next time."""
    path = _snapshot_path()
    payload = json.dumps(
        {
            "v": _SNAPSHOT_VERSION,
            "base_url": base_url,
            "written_at": time.time(),
            "values": values,
        },
        sort_keys=True,
    ).encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as file:
            fd = -1
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)  # noqa: PTH105 — explicit atomic replacement primitive
    except OSError:
        return
    finally:
        if fd != -1:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            temporary.unlink()


def _apply_bootstrap_values(base_url: str, values: dict[str, str]) -> None:
    """Inject one bootstrap payload into os.environ — the shared tail of the
    fetch path and the snapshot path."""
    # ``AVA_GATEWAY_HEALTH_URL`` is host-scoped, so the gateway correctly does
    # not serve it in the cluster bootstrap payload.  A pure runner that was
    # enrolled without an explicit override must still probe the gateway it
    # actually fetched from, not ServiceSettings' single-box localhost default.
    # Keep a deliberate host override; otherwise the enrollment base URL is the
    # one reachability fact this process already proved.
    if not os.environ.get("AVA_GATEWAY_HEALTH_URL"):
        os.environ["AVA_GATEWAY_HEALTH_URL"] = f"{base_url.rstrip('/')}/api/health"
    from base.host.env.dotenv_boot import is_delivered_unit_login

    delivered = is_delivered_unit_login()
    for key, value in values.items():
        if key == "AVA_DB_URL" and delivered:
            # The launcher's (or this process's consumed) unit login stays; the
            # served value is only the credential-free endpoint.
            continue
        os.environ[key] = value


def _gateway_base_url() -> str:
    """The enrolled gateway to dial, or an actionable failure.

    The gateway URL is AVA_GATEWAY_URL (`ava start` wrote it to `.env`; this
    runs before Settings, so it is read from the environment directly).
    """
    base_url = os.environ.get("AVA_GATEWAY_URL") or ""
    if not base_url:
        raise BootstrapFetchError(
            "this host is a pure agent-runner but has no AVA_GATEWAY_URL — its cluster "
            "config comes from the gateway at startup. Join it to its gateway first:\n"
            "    set AVA_DB_CAPABILITY_KEY from a non-echoing prompt, then run:\n"
            "    ava init --serve-agent-runner --no-serve-gateway --gateway-url <url> --machine-name <name> --machine-host "
            "<this-host-addr> --db-capability <bundle from `ava cluster db-authority issue-unit`>, then ava start"
        )
    return base_url


def resolve_bootstrap_values() -> dict[str, str]:
    """The cluster config values this process would run on — the boot resolution, read-only.

    The one resolution shared by `inject_config_from_gateway` (which applies
    the result to os.environ) and callers that must ask "would this host boot
    against resolvable config right now?" without mutating their process — the
    hold watchdog's pre-attempt gate (task #4080). The contract is the boot
    contract, unchanged:

    - a fresh snapshot (`_SNAPSHOT_FRESH_S`) is authoritative and skips the
      fetch entirely (P0 #2100);
    - otherwise a live fetch, refreshing the snapshot on success;
    - a transport failure falls back to the snapshot whatever its age — no
      cluster edit can have landed while the gateway was unreachable — with a
      logged warning; a non-transport failure (401/5xx) still fails loud.

    Raises:
        BootstrapFetchError: no gateway URL, or every fetch attempt failed — the
            process must not start with no config.
    """
    base_url = _gateway_base_url()
    snapshot = _read_config_snapshot(base_url)
    if snapshot is not None and snapshot[1] <= _SNAPSHOT_FRESH_S:
        # Fresh parent snapshot: the authoritative values this unit fetched
        # moments ago. Skip the fetch — a gateway that is down or restarting
        # must not block this process (P0 #2100).
        return snapshot[0]
    try:
        # The gateway itself never fetches (config_source_is_local), so every
        # fetch this module makes is a runner fetch; it carries no database
        # login (the unit capability does).
        values = fetch_bootstrap_config(base_url)
    except _transport_failures() as exc:
        if snapshot is not None:
            # Gateway unreachable: continue on the last-known cluster config
            # (the same values the parent process already holds). A gateway
            # outage is exactly when exec children must keep running, and no
            # cluster edit can have landed since the gateway went down.
            from base.log import logger

            logger.warning(
                "[bootstrap] gateway unreachable ({exc}) — continuing on the "
                "last-known cluster config snapshot (age {age:.0f}s)",
                exc=type(exc).__name__,
                age=snapshot[1],
            )
            return snapshot[0]
        raise BootstrapFetchError(
            f"could not fetch cluster config from the gateway at {base_url} ({exc}).\n"
            "    A pure agent-runner fetches GET /api/bootstrap at every process start "
            "(a fresh parent snapshot skips the fetch); this host has no snapshot to "
            "fall back on. Bring the gateway up (or check AVA_GATEWAY_URL / the "
            "unit capability's API token / private-network reachability), then retry."
        ) from exc
    except Exception as exc:
        raise BootstrapFetchError(
            f"could not fetch cluster config from the gateway at {base_url} ({exc}).\n"
            "    A pure agent-runner fetches GET /api/bootstrap at every process start "
            "(a fresh parent snapshot skips the fetch). Bring the gateway up (or check "
            "AVA_GATEWAY_URL / the unit capability's API token / private-network "
            "reachability), then retry."
        ) from exc
    _write_config_snapshot(base_url, values)
    return values


def inject_config_from_gateway() -> None:
    """Fetch the cluster's config from the gateway and inject it into os.environ.

    Runs at Settings construction on a pure agent-runner (see
    `config_source_is_local`). The fetched values are AUTHORITATIVE: every key
    the gateway serves overwrites os.environ, including a stale value a
    pre-cutover `.env` still materializes (`_enforce_cluster_env_authority`
    forces file values in at `load_ava_env`, and this runs after it) and a
    forwarded copy from a spawning process. The gateway's `.env` is the single
    cluster-wide copy.

    The values come from `resolve_bootstrap_values` (fresh snapshot / fetch /
    last-known-snapshot fallback); this wrapper applies them, including the
    derived `AVA_GATEWAY_HEALTH_URL` for a runner enrolled without an explicit
    override. The database login is the one exception to "fetched values are
    authoritative": a delivered unit login is kept, and a process without one
    receives the installed capability only when it runs the admitted runtime
    (`base.host.env.dotenv_boot.deliver_unit_authority`). That delivery runs first:
    it also supplies the machine API token the fetch authenticates with.
    """
    from base.host.env.dotenv_boot import deliver_unit_authority

    base_url = _gateway_base_url()
    deliver_unit_authority()
    values = resolve_bootstrap_values()
    _apply_bootstrap_values(base_url, values)
