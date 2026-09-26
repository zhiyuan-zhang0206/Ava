"""Settings-free input boundary of ``ava start``.

This is the first phase of start, not a separately callable initializer. Runtime
Settings are imported only after this home's durable identity is complete.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from dotenv import dotenv_values

from cli.start_identity import IdentityInput, prepare_identity, read_intent
from cli.start_runtime import StartRuntime
from shared.env_registry import (
    WSL_DEFAULT_HEALTH_PORT_BASE,
    derived_env_keys,
    env_identity_keys,
    health_port_env,
)
from shared.netutil import is_loopback_host
from shared.platform import IS_WINDOWS, IS_WSL, file_lock
from shared.private_storage import ensure_private_dir

_CAP_ARGS = {
    "gateway": "serve_gateway",
    "agent-runner": "serve_agent_runner",
    "observability-station": "serve_observability_station",
}
_FIELDS = ("machine_name", "machine_host", "machine_description", "memory_remote", "gateway_url")


def _checkout() -> Path:
    return Path(__file__).resolve().parents[1]


def _home(*, worktree: bool, runtime: StartRuntime | None = None) -> Path:
    if runtime is not None and runtime.release is not None:
        return _release_home(runtime, worktree=worktree)
    checkout = _checkout()
    pointer = checkout / ".ava_home"
    explicit = os.environ.get("AVA_HOME")
    target = Path(explicit).expanduser() if explicit else None
    claimed = Path(pointer.read_text().strip()).expanduser() if pointer.exists() else None
    default = Path.home() / ".ava"
    if target is None:
        target = claimed or (Path.home() / f".ava-{checkout.name}" if worktree else None)
    if target is None and checkout == (default / "source").resolve():
        target = default
    if target is None:
        raise ValueError(
            "unanchored start requires AVA_HOME or --worktree; it cannot select production implicitly"
        )
    return _validate_home(target, claimed, checkout, default, worktree=worktree)


def _release_home(runtime: StartRuntime, *, worktree: bool) -> Path:
    if worktree or runtime.home is None:
        raise ValueError("verified release start cannot create a worktree identity")
    explicit = os.environ.get("AVA_HOME")
    if explicit and Path(explicit).expanduser().resolve() != runtime.home:
        raise ValueError("AVA_HOME contradicts the admitted release home")
    runtime.validate(runtime.home)
    return runtime.home


def _validate_home(
    target: Path, claimed: Path | None, checkout: Path, default: Path, *, worktree: bool
) -> Path:
    if not target.is_absolute():
        raise ValueError("AVA_HOME must be absolute")
    target = target.resolve()
    if claimed is not None and claimed.resolve() != target:
        raise ValueError("AVA_HOME contradicts this checkout's .ava_home")
    if target == default.resolve() and checkout != (default / "source").resolve():
        raise ValueError(
            "the production home must be started from its canonical source or verified release"
        )
    if worktree and target == default.resolve():
        raise ValueError("--worktree cannot use the production home")
    return target


def _stored(home: Path) -> dict[str, str]:
    values = {k: v for k, v in dotenv_values(home / ".env").items() if v is not None}
    for field in (*_FIELDS, *("machine_" + v for v in _CAP_ARGS.values())):
        path = home / field
        key = "AVA_" + field.upper()
        if key not in values and path.exists():
            values[key] = path.read_text().strip()
    intent = read_intent(home)
    if intent is not None and intent["phase"] == "claiming":
        values.update(intent["env"])
    return values


def _roles(args: argparse.Namespace, stored: dict[str, str]) -> frozenset[str]:
    roles: set[str] = set()
    for cap, arg in _CAP_ARGS.items():
        selected = _capability_value(cap, stored, explicit=getattr(args, arg))
        if selected is None:
            selected = args.worktree and cap in {"gateway", "agent-runner"}
        if selected:
            roles.add(cap)
    if not roles:
        raise ValueError("first start requires explicit --serve-* capabilities or --worktree")
    if "gateway" in roles and IS_WINDOWS:
        raise ValueError("native Windows cannot host gateway; use WSL2 or join a POSIX gateway")
    return frozenset(roles)


def _capability_value(cap: str, stored: dict[str, str], *, explicit: bool | None) -> bool | None:
    key = "AVA_MACHINE_" + _CAP_ARGS[cap].upper()
    raw = stored.get(key)
    if raw is not None and raw.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError(f"invalid capability value {key}")
    prior = raw.lower() in {"true", "1", "yes", "on"} if raw is not None else None
    if explicit is not None and prior is not None and explicit != prior:
        raise ValueError(f"start cannot change persisted capability {cap}")
    return prior if prior is not None else explicit


def _join(values: dict[str, str]) -> None:
    from shared.bootstrap import fetch_bootstrap_config

    gateway = values["AVA_GATEWAY_URL"]
    host = values.get("AVA_MACHINE_HOST", "")
    secret = values.get("AVA_CLUSTER_SECRET", "")
    remote = not is_loopback_host(urlsplit(gateway).hostname or "")
    if remote and (not host or is_loopback_host(host)):
        raise ValueError("joining a remote gateway requires a reachable --machine-host")
    if remote and not secret:
        raise ValueError("joining a remote gateway requires AVA_CLUSTER_SECRET")
    os.environ["AVA_CLUSTER_SECRET"] = secret
    payload = fetch_bootstrap_config(gateway, role="runner")
    from shared.config.data_plane import _is_runner_db_url

    if not _is_runner_db_url(payload["AVA_DB_URL"]):
        raise ValueError("gateway did not return the runner database credential projection")
    if remote and any(
        is_loopback_host(urlsplit(payload[key]).hostname or "")
        for key in ("AVA_DB_URL", "AVA_REDIS_URL")
    ):
        raise ValueError("remote gateway returned loopback data-plane URLs")
    # Verify connection facts without persisting a gateway-owned configuration cache.
    for key in payload:
        values.pop(key, None)


def _config_values(args: argparse.Namespace, home: Path) -> tuple[dict[str, str], str | None]:
    if args.config_file is None:
        return {}, None
    from shared.config_lite_table import FIELD_ALIASES

    path = Path(args.config_file).expanduser().resolve(strict=True)
    if path.is_relative_to(home):
        raise ValueError("first-start config file must be outside the home")
    content = path.read_bytes()
    values = dotenv_values(stream=io.StringIO(content.decode()), interpolate=False)
    remote_keys = {"AVA_DB_URL", "AVA_REDIS_URL", "AVA_RUNNER_DB_PASSWORD"}
    forbidden = (
        derived_env_keys()
        | env_identity_keys()
        | {
            "AVA_HOME",
            "AVA_HOME_OVERRIDE",
            "AVA_CLUSTER_REGISTRY",
            "AVA_REDIS_ADMIN_PASSWORD",
        }
    ) - remote_keys
    unknown = set(values) - set(FIELD_ALIASES.values()) - remote_keys
    if unknown or set(values) & forbidden or any(v is None for v in values.values()):
        raise ValueError("config file contains unknown, identity, resource, or valueless keys")
    remote = set(values) & remote_keys
    if remote and (remote != remote_keys or any(not values[key] for key in remote_keys)):
        raise ValueError("remote config file requires paired DB/Redis URLs and runner credential")
    digest = hashlib.sha256(content).hexdigest()
    _validate_config_retry(home, digest)
    return {k: v for k, v in values.items() if v is not None}, digest


def _validate_config_retry(home: Path, digest: str) -> None:
    data = read_intent(home)
    if data is not None and data["config_digest"] != digest:
        raise ValueError("config file differs from the persisted first-start input")
    if data is None and (home / ".env").exists():
        raise ValueError("config file is only accepted with a journal-owned first start")


def _validate_remote_inputs(values: dict[str, str], roles: frozenset[str]) -> None:
    if "AVA_DB_URL" not in values:
        return
    if "gateway" not in roles:
        raise ValueError("remote data-plane configuration belongs to a gateway unit")
    endpoints = _remote_endpoints(values)
    local = _local_addresses(values.get("AVA_MACHINE_HOST", ""))
    for endpoint in endpoints:
        _require_foreign_endpoint(endpoint, local)


def _local_addresses(machine_host: str) -> set[str]:
    import socket

    import psutil

    local = {socket.gethostname().lower(), socket.getfqdn().lower(), machine_host.lower()}
    for addresses in psutil.net_if_addrs().values():
        local.update(a.address.split("%", 1)[0].lower() for a in addresses)
    return local


def _remote_endpoints(values: dict[str, str]) -> tuple[SplitResult, SplitResult]:
    from urllib.parse import parse_qs

    from psycopg.conninfo import conninfo_to_dict

    db = values["AVA_DB_URL"]
    parts = urlsplit(db)
    if (
        parts.scheme not in {"postgres", "postgresql"}
        or not parts.username
        or not parts.path.strip("/")
        or parts.username == "ava_runner"
    ):
        raise ValueError("remote database config requires an explicit database owner URL")
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    if set(query) & {"host", "hostaddr", "port", "service", "dbname", "user", "password"}:
        raise ValueError(
            "remote database URL cannot redirect its identity through query parameters"
        )
    conninfo_to_dict(db)
    redis = urlsplit(values["AVA_REDIS_URL"])
    if redis.scheme not in {"redis", "rediss"}:
        raise ValueError("remote Redis config requires a Redis URL")
    return parts, redis


def _require_foreign_endpoint(endpoint: SplitResult, local: set[str]) -> None:
    import socket

    host = (endpoint.hostname or "").lower()
    if not host or is_loopback_host(host) or host in local:
        raise ValueError("remote data-plane config must name foreign hosts for both services")
    addresses = socket.getaddrinfo(host, endpoint.port or 0, type=socket.SOCK_STREAM)
    if any(is_loopback_host(str(a[4][0])) or str(a[4][0]).lower() in local for a in addresses):
        raise ValueError("remote data-plane endpoint resolves to this host")


def _inputs(
    args: argparse.Namespace, home: Path, runtime: StartRuntime | None = None
) -> IdentityInput:
    stored = _stored(home)
    roles = _roles(args, stored)
    values: dict[str, str] = dict(stored)
    config, digest = _config_values(args, home)
    values.update(config)
    values["AVA_SERVICE_PATH"] = _service_path(values, home)
    _apply_identity_options(args, home, stored, values, roles)
    if config.get("AVA_DB_URL"):
        _validate_remote_inputs(values, roles)
    if "gateway" not in roles:
        if not values.get("AVA_GATEWAY_URL"):
            raise ValueError("first remote-unit start requires --gateway-url")
        values.setdefault("AVA_CLUSTER_SECRET", os.environ.get("AVA_CLUSTER_SECRET", ""))
        _join(values)
    registry = (
        Path(
            os.environ.get("AVA_CLUSTER_REGISTRY")
            or stored.get("AVA_CLUSTER_REGISTRY")
            or str(Path.home() / ".ava" / "clusters.json")
        )
        .expanduser()
        .resolve()
    )
    checkout = _checkout()
    if runtime is not None and runtime.release is not None:
        intent = read_intent(home)
        if intent is None:
            raise ValueError("release start has no existing home identity")
        checkout = Path(intent["checkout"])
    return IdentityInput(home, registry, checkout, args.worktree, roles, values, digest, runtime)


def _service_path(values: dict[str, str], home: Path) -> str:
    from shared.session_env import admit_service_path

    if "AVA_SERVICE_PATH" in values:
        return admit_service_path(values["AVA_SERVICE_PATH"])
    if (home / ".env").exists() or read_intent(home) is not None:
        raise ValueError("existing home requires an explicit AVA_SERVICE_PATH declaration")
    if "AVA_SERVICE_PATH" in os.environ:
        return admit_service_path(os.environ["AVA_SERVICE_PATH"])
    bin_name = "Scripts" if IS_WINDOWS else "bin"
    venvs = [_checkout() / ".venv"]
    if sys.prefix != sys.base_prefix:
        venvs.append(Path(sys.prefix))
    if activated := os.environ.get("VIRTUAL_ENV"):
        venvs.append(Path(activated))
    return admit_service_path(
        os.environ.get("PATH", ""), excluded=tuple(venv / bin_name for venv in venvs)
    )


def _apply_identity_options(
    args: argparse.Namespace,
    home: Path,
    stored: dict[str, str],
    values: dict[str, str],
    roles: frozenset[str],
) -> None:
    for field in _FIELDS:
        explicit = getattr(args, field)
        key = "AVA_" + field.upper()
        if explicit is not None:
            if key in stored and stored[key] != explicit:
                raise ValueError(f"start input conflicts with persisted {key}")
            values[key] = explicit
    if args.worktree:
        values.setdefault("AVA_MACHINE_NAME", home.name.lstrip("."))
        values.setdefault("AVA_HEALTH_PROBE_AGENT_MIN", "0")
    if not values.get("AVA_MACHINE_NAME"):
        raise ValueError("first start requires --machine-name")
    for key in ("AVA_MACHINE_NAME", "AVA_MACHINE_HOST"):
        if any(c in values.get(key, "") for c in "\r\n\x00"):
            raise ValueError(f"invalid {key}")
    _apply_host_options(args, values, roles)


def _apply_host_options(
    args: argparse.Namespace, values: dict[str, str], roles: frozenset[str]
) -> None:
    if args.health_port_base is not None:
        values.update(health_port_env(args.health_port_base))
    elif IS_WSL and "gateway" not in roles:
        for key, value in health_port_env(WSL_DEFAULT_HEALTH_PORT_BASE).items():
            values.setdefault(key, value)
    if args.ssl_cert_file is not None:
        values.update(SSL_CERT_FILE=args.ssl_cert_file, REQUESTS_CA_BUNDLE=args.ssl_cert_file)
        os.environ["SSL_CERT_FILE"] = args.ssl_cert_file


def _prepare_start_locked(
    args: argparse.Namespace, home: Path, runtime: StartRuntime | None = None
) -> None:
    inputs = _inputs(args, home, runtime)
    prepare_identity(inputs)
    for key in derived_env_keys() | env_identity_keys():
        os.environ.pop(key, None)
    os.environ["AVA_HOME"] = str(home)
    os.environ["AVA_CLUSTER_REGISTRY"] = str(inputs.registry)


def prepare_start(args: argparse.Namespace) -> Path:
    home = _home(worktree=args.worktree)
    StartRuntime.development(_checkout()).validate(home)
    ensure_private_dir(home)
    with file_lock(home / "start-intent.lock", timeout_s=30):
        _prepare_start_locked(args, home)
    return home


def run_start(args: argparse.Namespace, *, runtime: StartRuntime | None = None) -> int:
    try:
        if runtime is None:
            runtime = StartRuntime.development(_checkout())
        home = _home(worktree=args.worktree, runtime=runtime)
        runtime.validate(home)
        from shared.release_operation import require_start_authorized

        if runtime.release is not None:
            require_start_authorized(home)
        ensure_private_dir(home)
        with file_lock(home / "start-intent.lock", timeout_s=30):
            _prepare_start_locked(args, home, runtime)
            if runtime.release is not None:
                require_start_authorized(home)
            from cli.main import _init_detached_cli_logging

            _init_detached_cli_logging()
            from cli.commands.start import cmd_start

            result = cmd_start(
                disabled_services=tuple(args.disable_service),
                only_services=tuple(args.only_service),
                all_services=args.all_services,
                persist_services=args.persist_services,
                runtime=runtime,
            )
            if result == 0:
                from cli.commands._root_driver import complete_boot_start
                from shared.start_serving import clear_serving

                try:
                    complete_boot_start()
                except (RuntimeError, OSError, TimeoutError):
                    clear_serving()
                    raise
            return result
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            from cli.main import _print_settings_load_failure

            return _print_settings_load_failure(exc)
        print(f"ava start: {exc}", file=sys.stderr)
        return 1
