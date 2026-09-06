"""Settings-independent agent-runner enrollment.

`ava enroll` runs on a fresh host that has no full config yet, so this module
imports neither shared.config (building Settings would fail without the config)
nor cli.commands. It writes the small bootstrap env and verifies connectivity by
fetching config from the gateway, presenting the cluster secret from
`AVA_CLUSTER_SECRET` (preferred) or the compatibility `--cluster-secret` flag.
main() routes `enroll` here before the settings-gated command import.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from shared import bootstrap
from shared.browser_deps import browser_deps_notice, browser_deps_warning, ensure_browser_deps
from shared.dotenv_boot import AVA_ENV_PATH
from shared.env_registry import (
    WSL_DEFAULT_HEALTH_PORT_BASE,
    health_port_env,
    health_port_env_aliases,
)
from shared.envfile import ENV_LOCK_TIMEOUT_S, env_lock_path, snapshot_env, upsert_env
from shared.platform import IS_WSL, file_lock


def _replace_private_text(path: Path, content: str) -> None:
    """Atomically replace ``path`` with owner-only UTF-8 text.

    The temporary file is born 0600 in the target directory, so cleartext is
    never visible under the process umask and ``os.replace`` cannot expose a
    partial file if the process dies during the write.
    """
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _preserved_env_lines(path: Path, dropped_keys: frozenset[str]) -> list[str]:
    """The existing `.env` lines this enroll must carry forward, verbatim.

    Everything survives except `dropped_keys` — the keys the bootstrap block
    rewrites plus the cluster config keys enroll deliberately does not persist.
    Comments and blank lines are kept as-is: the file is operator-editable
    (windows-setup.md sends people here for model keys), so a re-enroll must not
    eat their notes along with their keys.
    """
    if not path.exists():
        return []
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in dropped_keys:
                continue
        kept.append(line)
    # Trim the edges so the block separator this writer adds is not itself
    # preserved and re-added on the next rewrite (byte-idempotence).
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def write_bootstrap_env(
    path: Path,
    *,
    gateway: str,
    machine_name: str,
    cluster_secret: str | None = None,
    ssl_cert_file: str | None = None,
    cluster_keys: frozenset[str] = frozenset(),
) -> None:
    """Write the bootstrap env for a gateway-sourced agent-runner, preserving
    every machine-local key it does not own.

    The bootstrap block is identity + reachability only — deliberately no
    cluster connection facts: the runner fetches those from the gateway at every
    process start (see the module docstring). The config source is role-derived
    (AVA_CONFIG_SOURCE is gone): a unit that does not serve the gateway fetches,
    so this file needs no source marker.

    `cluster_keys` is the key set the gateway's /api/bootstrap serves — the
    caller just fetched it to verify connectivity. Those keys are dropped from
    the existing file (a runner must not cache cluster facts: dotenv_boot's env
    authority would force a stale cached value, and a settings-lite process
    would dial it). Everything else — model API keys, AVA_REQUIRE_* opt-outs,
    a prior enroll's health-port block or SSL bundle — survives verbatim: a
    re-enroll used to REPLACE the whole file, which silently wiped this unit's
    model credentials and machine-scope opt-outs (fleet Windows box,
    2026-08-19). A key is only rewritten when this call restates it (the SSL
    pair, like the health-port block, keeps a prior explicit choice on a bare
    re-enroll).
    """
    owned = {"AVA_GATEWAY_URL", "AVA_MACHINE_NAME", "AVA_MACHINE_SERVE_AGENT_RUNNER"}
    lines = [
        f"AVA_GATEWAY_URL={gateway}",
        f"AVA_MACHINE_NAME={machine_name}",
        "AVA_MACHINE_SERVE_AGENT_RUNNER=true",
    ]
    if cluster_secret:
        # The runner presents this on GET /api/bootstrap (the gateway requires it
        # when multi-host is on) and on its own authenticated /ops.
        lines.append(f"AVA_CLUSTER_SECRET={cluster_secret}")
        owned.add("AVA_CLUSTER_SECRET")
    if ssl_cert_file:
        lines.append(f"SSL_CERT_FILE={ssl_cert_file}")
        lines.append(f"REQUESTS_CA_BUNDLE={ssl_cert_file}")
        owned.update({"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"})
    path.parent.mkdir(parents=True, exist_ok=True)
    # The fourth door onto `.env`: it rewrites the bootstrap block and drops the
    # cluster keys, carrying every other existing line forward. An enroll landing
    # while converge is mid-`upsert_env` would otherwise write over a half-written
    # file, or be written over by one. Same lock as every other door
    # (`shared.envfile.env_lock_path`).
    with file_lock(env_lock_path(path), timeout_s=ENV_LOCK_TIMEOUT_S):
        preserved = _preserved_env_lines(path, frozenset(owned) | cluster_keys)
        if preserved:
            lines += ["", *preserved]
        snapshot_env(path)
        _replace_private_text(path, "\n".join(lines) + "\n")


def _enroll_cluster_secret(parser: argparse.ArgumentParser, explicit: str | None) -> str:
    """Resolve and validate the required enrollment bearer secret.

    Environment injection is the documented route because it keeps the secret
    out of argv and shell history. The flag remains for compatibility with
    existing automation, with explicit input taking precedence over the env.
    """
    secret = explicit if explicit is not None else os.environ.get("AVA_CLUSTER_SECRET", "")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")
    if not secret:
        parser.error(
            "set AVA_CLUSTER_SECRET in the environment (preferred), or pass "
            "--cluster-secret for compatibility"
        )
    if not set(secret) <= allowed:
        parser.error("AVA_CLUSTER_SECRET must be a URL-safe token (letters, digits, and '._~-')")
    return secret


def _existing_health_port_env(path: Path) -> dict[str, str]:
    """The `AVA_*_HEALTH_PORT` keys already present in this unit's `.env`, if any.

    `write_bootstrap_env` carries these forward (host-scope keys it does not
    own), so their survival across a bare re-enroll is structural; this read
    exists to RESOLVE the base — an existing block means "keep as-is" beats the
    WSL2 auto-default — and to report the outcome. Empty when the file is
    absent or carries no health-port key.
    """
    if not path.exists():
        return {}
    keys = set(health_port_env_aliases().values())
    found: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in keys:
            found[key] = value
    return found


def _resolve_health_port_base(explicit: int | None, existing: dict[str, str]) -> int | None:
    """The base to derive this enroll call's health-port block from, or None to
    leave it as-is (either genuinely unset, or restored verbatim from `existing`
    by the caller — see `run_enroll`).

    Precedence: an explicit `--health-port-base` always wins; failing that, a
    block already present in `.env` is preserved as-is (returns None so the
    caller restores the original key/value pairs rather than re-deriving them —
    a hand-edited or partial block may not sit on a clean block-grid base);
    failing that, a WSL2 host (issue #1152) auto-defaults to
    `WSL_DEFAULT_HEALTH_PORT_BASE` instead of the shared default a co-located
    native Windows unit would also fall into. Every other host leaves the block
    unset, unchanged from before this fix.
    """
    if explicit is not None:
        return explicit
    if existing:
        return None
    if IS_WSL:
        return WSL_DEFAULT_HEALTH_PORT_BASE
    return None


def _print_health_port_outcome(
    *, explicit: int | None, resolved: int | None, existing: dict[str, str]
) -> None:
    """The one health-port status line `run_enroll` prints, phrased for which of
    `_resolve_health_port_base`'s branches fired."""
    if explicit is not None:
        ports = health_port_env(explicit)
        print(
            f"daemon health ports pinned to this unit's own block at {explicit}: "
            f"{', '.join(f'{k}={v}' for k, v in sorted(ports.items()))}"
        )
    elif resolved is not None:
        ports = health_port_env(resolved)
        print(
            f"daemon health ports auto-defaulted to base {resolved} (this host is "
            f"WSL2 — issue #1152; pass --health-port-base to pick a different one): "
            f"{', '.join(f'{k}={v}' for k, v in sorted(ports.items()))}"
        )
    elif existing:
        print(
            "kept this unit's existing daemon health-port block: "
            f"{', '.join(f'{k}={v}' for k, v in sorted(existing.items()))}"
        )


def _browser_explicitly_disabled() -> bool:
    """Return whether this unit deliberately disables the shared browser service."""
    if not AVA_ENV_PATH.exists():
        return False
    for line in AVA_ENV_PATH.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "AVA_BROWSER_ENABLED":
            # Keep this raw bootstrap check aligned with Pydantic's bool coercion.
            return value.strip().lower() in {"false", "0", "no", "off", "f", "n"}
    return False


def _check_browser_deps() -> None:
    """Detect and repair ava-browser dependencies, then report any remaining gap.

    Enroll must not leave a host that silently skips ava-browser forever
    (company-mini, 2026-08-27: no npx, skipped since). Never fails enroll —
    identity and connectivity are enroll's job, and a headless host legitimately
    cannot run a browser. Repair warnings reach both output streams because
    headless enroll callers may capture only one; a display-less host instead
    receives a stdout-only informational notice because it needs no repair.
    """
    if _browser_explicitly_disabled():
        return
    reason = ensure_browser_deps()
    if reason is None:
        print("browser deps OK (Chrome + npx on PATH) — ava-browser should start on this host")
        return
    if reason.startswith("no display"):
        print(browser_deps_notice(reason))
        return
    warning = browser_deps_warning(reason)
    print(warning)
    print(warning, file=sys.stderr)


def _payload_serves_loopback_data_plane(payload: dict[str, str]) -> bool:
    """True when the bootstrap payload's db/redis URLs carry loopback hosts.

    The gateway serves its own .env URLs with loopback hosts rewritten to its
    reachable address (`shared.config._serve_reachable_data_plane_hosts`); when
    that address is unset the served URLs keep loopback. A REMOTE runner dialing
    them would hit itself — the anchor model (deploy configures the gateway URL;
    db/redis hosts derive from the gateway's machine_host) fails loud here
    instead of at first DB connect."""
    from urllib.parse import urlsplit

    from shared.netutil import is_loopback_host

    for alias in ("AVA_DB_URL", "AVA_REDIS_URL"):
        host = urlsplit(payload.get(alias, "")).hostname or ""
        if host and is_loopback_host(host):
            return True
    return False


def _fetch_enroll_payload(gateway: str) -> dict[str, str]:
    """Fetch the bootstrap config payload from the gateway.

    Separated from _persist_enroll so tests can monkeypatch this call.

    The fetch requests the RUNNER credential projection: this host is enrolling
    as an agent-runner, so its AVA_DB_URL must be the least-privilege
    ava_runner one (Task #1236) — exactly what every later process-start fetch
    (`shared.bootstrap.inject_config_from_gateway`) will request too.

    Args:
        gateway: gateway base URL.

    Returns:
        The raw {ENV_ALIAS: value} dict from GET /api/bootstrap.

    Raises:
        httpx.HTTPStatusError: non-2xx.
        httpx.HTTPError: transport failure (gateway unreachable).
    """
    return bootstrap.fetch_bootstrap_config(gateway, role="runner")


def _persist_enroll(
    home: Path,
    *,
    machine_name: str,
    health_port_base: int | None = None,
) -> None:
    """Persist machine identity into the agent-runner home after a successful fetch.

    The cluster's connection facts are NOT written here — the 2026-08-01 refactor
    deleted the materialized cache; the runner fetches them from the gateway at
    every process start. Only this unit's own host-scope facts are persisted:
    AVA_MACHINE_NAME (idempotent upsert, so the `.env` stays the identity source)
    and the daemon health-port block when `health_port_base` is given. The gateway
    does not serve health ports (a port block is a per-unit fact — see the module
    docstring), so nothing here can collide with a fetched value.

    Args:
        home: the agent-runner's $AVA_HOME directory (created if missing).
        machine_name: written as AVA_MACHINE_NAME (idempotent upsert).
        health_port_base: this unit's own daemon health-port block base, or None to
            leave the ports unset (each daemon takes its shared default).

    Raises:
        ValueError: `health_port_base` derives a port outside 1024-65535.
    """
    home.mkdir(parents=True, exist_ok=True)
    unit_env = {"AVA_MACHINE_NAME": machine_name}
    if health_port_base is not None:
        unit_env.update(health_port_env(health_port_base))
    upsert_env(home / ".env", unit_env)


def _http_error_detail(exc: Exception) -> str | None:
    """The gateway's JSON `detail` from an httpx.HTTPStatusError, else None."""
    import httpx

    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    try:
        detail = exc.response.json().get("detail")
    except Exception:
        return None
    return detail if isinstance(detail, str) else None


def run_enroll(argv: list[str]) -> int:
    """Parse enroll args; verify the gateway via a fetch, then write the bootstrap env."""
    parser = argparse.ArgumentParser(
        prog="ava enroll",
        description="enroll this host as an agent-runner (writes bootstrap env)",
    )
    parser.add_argument("--gateway", required=True, help="gateway URL")
    parser.add_argument("--machine-name", required=True)
    parser.add_argument(
        "--machine-host",
        required=True,
        help="this host's reachable address on the cluster's private network (e.g. its "
        "VPN overlay IP / private hostname) — the gateway dials this agent-runner's ops server "
        "here. localhost only works when the gateway shares this box.",
    )
    parser.add_argument(
        "--cluster-secret",
        default=None,
        help="compatibility input for the gateway cluster secret; prefer setting "
        "AVA_CLUSTER_SECRET in the environment so the secret does not appear in argv. "
        "Required by either source: every "
        "enrollable gateway is a split deployment, and a split deployment always "
        "has a secret (a gateway-only birth mints one; remote agent-runners "
        "depend on it for scram/requirepass + bearer auth). The runner presents "
        "it to the gateway's authenticated /api/bootstrap and on its own /ops.",
    )
    parser.add_argument(
        "--ssl-cert-file", default=None, help="local CA bundle (corp TLS-MITM hosts)"
    )
    parser.add_argument(
        "--health-port-base",
        type=int,
        default=None,
        help="this unit's own daemon health-port block base, on the allocator's grid "
        "(18000 + k*16, e.g. 18112 -> ops 18119). Pass it when another Ava "
        "unit shares this machine's localhost namespace — a second install, or a WSL2 distro "
        "whose loopback Windows can reach — since both units otherwise take the same shared "
        "defaults (the shared 8102-8111 segment). Pick a base no local cluster already owns (`ava cluster ls`); "
        "nothing checks it against the registry. Omit it on a machine that carries one unit. "
        "On WSL2, omitting it does not mean unset: enroll auto-applies a fixed reserved base "
        "(issue #1152) so a WSL2 unit never defaults into the same bucket a co-located native "
        "Windows unit would; pass this flag to pick a different one instead.",
    )
    args = parser.parse_args(argv)
    cluster_secret = _enroll_cluster_secret(parser, args.cluster_secret)

    # Remote enrollment requires a reachable machine-host: if the gateway is on
    # another box (non-loopback URL) but --machine-host is loopback, the gateway
    # would later dial this runner at localhost and hit itself, silently reporting
    # it online under the wrong identity. Reject before writing any bootstrap
    # state so the misconfiguration fails at enroll, not three weeks later.
    from urllib.parse import urlparse

    from shared.netutil import is_loopback_host

    gateway_host = urlparse(str(args.gateway)).hostname or ""
    if not is_loopback_host(gateway_host) and is_loopback_host(args.machine_host):
        print(
            f"enroll FAILED: remote enrollment requires a reachable machine-host, but "
            f"--machine-host {args.machine_host!r} is loopback. The gateway at {args.gateway} "
            f"must dial this runner over the network — pass this host's private-network "
            f"address (VPN overlay IP / private hostname) as --machine-host.",
            file=sys.stderr,
        )
        return 1

    # Validate the base before any state is written, for the same reason as the
    # machine-host check above: a typo'd base must fail at enroll rather than
    # leave a half-written home behind.
    if args.health_port_base is not None:
        try:
            health_port_env(args.health_port_base)
        except ValueError as exc:
            print(f"enroll FAILED: {exc}", file=sys.stderr)
            return 1

    # Any health-port block THIS unit already carries: an existing block wins
    # over the WSL2 auto-default on a bare re-enroll (no --health-port-base this
    # time). write_bootstrap_env preserves the keys themselves.
    existing_health_ports = _existing_health_port_env(AVA_ENV_PATH)
    health_port_base = _resolve_health_port_base(args.health_port_base, existing_health_ports)

    # The verification fetch reads AVA_CLUSTER_SECRET from os.environ (it runs
    # before this host's .env is loaded into the environment), so mirror it.
    os.environ["AVA_CLUSTER_SECRET"] = cluster_secret

    # Verify connectivity BEFORE any bootstrap state is written: enroll must not
    # leave a half-written home when the gateway is unreachable or the secret is
    # wrong (a bootstrap .env is what the installed-home gate treats as enrolled).
    try:
        payload = _fetch_enroll_payload(args.gateway)
    except Exception as exc:
        # Surface the gateway's own explanation (the JSON `detail`) — e.g. a 401
        # "cluster secret required" on a wrong/missing secret — instead of the
        # bare httpx status line.
        detail = _http_error_detail(exc)
        if detail:
            print(f"enroll FAILED: {detail}", file=sys.stderr)
        else:
            print(f"enroll verification FAILED: could not fetch config: {exc}", file=sys.stderr)
        return 1

    # URL-anchor guard: a remote gateway must serve this runner reachable
    # data-plane URLs. The db/redis URL hosts are derived from the GATEWAY's
    # machine_host at bootstrap time — when that is unset the gateway serves
    # loopback URLs, which a remote runner would dial as ITS OWN loopback and
    # hit itself. The gateway URL is non-loopback (checked above), so loopback
    # data-plane URLs can never be right; refuse now with the fix, instead of
    # weeks of silent DB failures on the runner.
    if _payload_serves_loopback_data_plane(payload):
        print(
            "enroll FAILED: the gateway serves loopback data-plane URLs — its "
            "AVA_MACHINE_HOST is unset, so a remote runner would dial its OWN "
            "loopback instead of the gateway's pg/redis. Set AVA_MACHINE_HOST "
            "(or $AVA_HOME/machine_host) on the gateway box to its private-network "
            "address, then re-run enroll.",
            file=sys.stderr,
        )
        return 1

    write_bootstrap_env(
        AVA_ENV_PATH,
        gateway=args.gateway,
        machine_name=args.machine_name,
        cluster_secret=cluster_secret,
        ssl_cert_file=args.ssl_cert_file,
        # The keys the gateway serves ARE the cluster config a runner must not
        # cache — drop them from the existing file; everything else survives.
        cluster_keys=frozenset(payload),
    )
    # machine_host is this host's stable, operator-declared reachable address — a
    # local identity, not gateway-sourced config. It lives in the
    # `$AVA_HOME/machine_host` file that reachable_host() reads, not the .env,
    # so it is independent of any .env writer.
    # Without it the agent-runner registers its ops endpoint at localhost and the
    # gateway dials itself instead of the runner.
    (AVA_ENV_PATH.parent / "machine_host").write_text(args.machine_host + "\n")
    print(f"wrote bootstrap env to {AVA_ENV_PATH}")

    _persist_enroll(
        AVA_ENV_PATH.parent,
        machine_name=args.machine_name,
        health_port_base=health_port_base,
    )
    print(
        f"verified gateway connectivity: fetched {len(payload)} cluster config values from "
        f"{args.gateway} (the runner re-fetches them at every process start — nothing is cached)."
    )
    _print_health_port_outcome(
        explicit=args.health_port_base, resolved=health_port_base, existing=existing_health_ports
    )
    _check_browser_deps()
    print("next: run `ava start` on this host.")
    return 0
