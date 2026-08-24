"""Fetch cluster-common config from the gateway at process start.

Every process on a configured pure agent-runner (serve_agent_runner flag on,
enrolled with a gateway URL) fetches the cluster's config from the gateway at
startup and injects it into os.environ before Settings is built — there is no
materialized `.env` cache of cluster facts anymore (retired 2026-08-01). A
gateway-capable unit keeps the cluster's config in its own `$AVA_HOME/.env` and
never fetches; which side a unit is on is derived from its serve flags (see
`config_source_is_local` / `should_fetch_from_gateway`), not from an env var
(AVA_CONFIG_SOURCE deleted). A bare checkout with no role flags (CI, lint
scripts) and a not-yet-enrolled runner resolve locally with no fetch — the
preflight gate refuses an unenrolled `ava start`, not the Settings import. The
bytes travel the private network; when multi-host is on the gateway requires
the cluster secret as a bearer token, which this presents from AVA_CLUSTER_SECRET.
Intentionally imports nothing from shared.config (it runs DURING shared.config
import) — only stdlib + httpx + shared.cluster_auth + shared.http_dial +
shared.netutil (all pure stdlib / config-free, so they're safe this early in boot).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, cast

import httpx

from shared.cluster_auth import bearer_header
from shared.http_dial import get as dial_get

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

# The maintenance-verb opt-out (settings-lite). `ava stop` / `ava status` / the
# watchdog probe / the thin-client verbs must still construct Settings while the
# gateway is down, so `cli.main` sets AVA_CONFIG_FETCH=skip for them before any
# settings-loading import. Every other process — start, converge, update, the
# daemons, the agents — derives its fetch decision purely from the role flag and
# fetches when it is a pure agent-runner. shared.session_env deliberately does NOT
# forward this var to spawned processes (a daemon/agent must fetch per its own
# role, never inherit a CLI verb's opt-out).
CONFIG_FETCH_ENV = "AVA_CONFIG_FETCH"
CONFIG_FETCH_SKIP = "skip"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _serve_flag(env_key: str, file_name: str) -> bool:
    """Settings-free mirror of one capability flag's resolution: env
    `AVA_MACHINE_SERVE_*` > `$AVA_HOME/<file>` > False (shared.machine's
    `_resolve_serve` reads the flag through Settings, which does not exist yet
    while this module runs). A malformed value resolves False here; the real
    resolver raises on it loudly later, so the config-source decision can never
    silently disagree with the machine role.
    """
    raw = os.environ.get(env_key)
    if raw is not None and raw.strip():
        return raw.strip().lower() in _TRUTHY
    path = Path(os.environ.get("AVA_HOME") or Path.home() / ".ava").expanduser() / file_name
    if path.exists():
        return path.read_text().strip().lower() in _TRUTHY
    return False


def config_source_is_local() -> bool:
    """Whether this unit's config source is its own `$AVA_HOME/.env`.

    Role-derived (AVA_CONFIG_SOURCE deleted 2026-08-01): a unit that serves the
    gateway owns the cluster's config in its own `.env` and never fetches; a
    configured pure agent-runner holds only the bootstrap env (gateway URL +
    secret, from `ava enroll`) and fetches the rest from the gateway at every
    process start (see `should_fetch_from_gateway`). A bare checkout with no
    role flags — CI, lint scripts, dev tools — is not a unit yet and resolves
    locally with no fetch.

    The serve-gateway flag is read settings-free (env > `$AVA_HOME/
    machine_serve_gateway` file > False) because this runs DURING the Settings
    import, before shared.machine can resolve the role. `cli.main` opts the
    maintenance verbs out of the fetch with AVA_CONFIG_FETCH=skip (see
    CONFIG_FETCH_ENV); that is orthogonal to this derivation.
    """
    return _serve_flag("AVA_MACHINE_SERVE_GATEWAY", "machine_serve_gateway")


def should_fetch_from_gateway() -> bool:
    """Whether this process's Settings build fetches cluster config from the gateway.

    True only for a CONFIGURED pure agent-runner: the serve_agent_runner flag is
    on (env `AVA_MACHINE_SERVE_AGENT_RUNNER` > `$AVA_HOME/machine_serve_agent_runner`
    file > False) AND a gateway URL is present (the host enrolled — `ava enroll`
    writes both together, fetch-first). Everything else resolves locally:

    - a gateway-capable unit (config_source_is_local) never fetches;
    - a bare checkout with no role flags (CI, lint scripts, dev tools) and a
      not-yet-enrolled runner (flag on, no URL) construct Settings from their
      local env/.env with no fetch and no error — `ava start`'s preflight gate
      is what refuses an unenrolled runner, not the Settings import, so any
      tool that imports shared.config keeps working on any machine.

    Reads os.environ only: by the time shared.config calls this, load_ava_env
    has loaded (and `_enforce_cluster_env_authority` forced) the unit's .env,
    so the flag and the URL are both present in the environment when they
    exist on disk.
    """
    return _serve_flag("AVA_MACHINE_SERVE_AGENT_RUNNER", "machine_serve_agent_runner") and bool(
        os.environ.get("AVA_GATEWAY_URL") or os.environ.get("AVA_PRIMARY_GATEWAY_URL")
    )


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


def fetch_bootstrap_config(
    base_url: str,
    timeout: float = _FETCH_TIMEOUT_S,
    attempts: int = _FETCH_ATTEMPTS,
    role: str | None = None,
) -> dict[str, str]:
    """GET {base_url}/api/bootstrap. Return {alias: value}.

    Retries with linear backoff so a gateway that is briefly unreachable as the
    agent boots doesn't strand it.

    `role` is the credential projection requested from the gateway: both
    ``"runner"`` and ``None`` receive the least-privilege `ava_runner`
    AVA_DB_URL. The main identity is never a bootstrap projection.

    Presents `Authorization: Bearer <AVA_CLUSTER_SECRET>` when that env var is set
    (enroll writes it on a split agent-runner); the gateway requires it when
    multi-host is on. Read from os.environ, not Settings — this runs during the
    Settings import.

    Raises:
        httpx.HTTPError: every attempt failed (gateway unreachable / non-2xx, e.g.
            401 when the cluster secret is missing or wrong).
        TypeError: the response body is not a flat ``{str: str}`` map.
    """
    secret = os.environ.get("AVA_CLUSTER_SECRET", "")
    headers = bearer_header(secret) if secret else {}
    url = f"{base_url.rstrip('/')}/api/bootstrap"
    if role:
        url = f"{url}?role={role}"
    attempt = 0
    while True:
        try:
            resp = dial_get(url, timeout=timeout, headers=headers)
            resp.raise_for_status()
            return _validated_bootstrap_payload(resp.json())
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Gateway briefly down (mid-restart): connect fails fast, so retrying
            # doesn't eat the timeout budget. A ReadTimeout propagates uncaught --
            # see the note on _FETCH_TIMEOUT_S for why it isn't retried.
            attempt += 1
            if attempt >= attempts:
                raise
            time.sleep(0.5 * attempt)


def inject_config_from_gateway() -> None:
    """Fetch the cluster's config from the gateway and inject it into os.environ.

    Runs at Settings construction on a pure agent-runner (see
    `config_source_is_local`). The fetched values are AUTHORITATIVE: every key
    the gateway serves overwrites os.environ, including a stale value a
    pre-cutover `.env` still materializes (`_enforce_cluster_env_authority`
    forces file values in at `load_ava_env`, and this runs after it) and a
    forwarded copy from a spawning process. The gateway's `.env` is the single
    cluster-wide copy; a runner holds no cache that could go stale, so a cluster
    edit reaches it on the next process restart.

    The gateway URL is AVA_GATEWAY_URL (enroll wrote it; the deprecated
    AVA_PRIMARY_GATEWAY_URL alias is honored too, since this runs before
    Settings resolves it).

    Raises:
        BootstrapFetchError: no gateway URL, or every fetch attempt failed — the
            process must not start with no config.
    """
    base_url = os.environ.get("AVA_GATEWAY_URL") or os.environ.get("AVA_PRIMARY_GATEWAY_URL")
    if not base_url:
        raise BootstrapFetchError(
            "this host is a pure agent-runner but has no AVA_GATEWAY_URL — its cluster "
            "config comes from the gateway at startup. Enroll it first:\n"
            "    set AVA_CLUSTER_SECRET from a non-echoing prompt, then run:\n"
            "    ava enroll --gateway <url> --machine-name <name> --machine-host "
            "<this-host-addr>"
        )
    try:
        # A runner process dials as the least-privilege ava_runner role (the
        # gateway projects AVA_DB_URL onto that credential — Task #1236). The
        # gateway itself never fetches (config_source_is_local), so every
        # fetch this module makes is a runner fetch.
        values = fetch_bootstrap_config(base_url, role="runner")
    except Exception as exc:
        raise BootstrapFetchError(
            f"could not fetch cluster config from the gateway at {base_url} ({exc}).\n"
            "    A pure agent-runner fetches GET /api/bootstrap at every process start — "
            "there is no cached copy. Bring the gateway up (or check AVA_GATEWAY_URL / "
            "the cluster secret / private-network reachability), then retry."
        ) from exc
    # ``AVA_GATEWAY_HEALTH_URL`` is host-scoped, so the gateway correctly does
    # not serve it in the cluster bootstrap payload.  A pure runner that was
    # enrolled without an explicit override must still probe the gateway it
    # actually fetched from, not ServiceSettings' single-box localhost default.
    # Keep a deliberate host override; otherwise the enrollment base URL is the
    # one reachability fact this process already proved.
    if not os.environ.get("AVA_GATEWAY_HEALTH_URL"):
        os.environ["AVA_GATEWAY_HEALTH_URL"] = f"{base_url.rstrip('/')}/api/health"
    for key, value in values.items():
        os.environ[key] = value
