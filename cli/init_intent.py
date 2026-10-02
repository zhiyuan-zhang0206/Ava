"""Settings-free input boundary of ``ava init``.

Init records one home's identity and nothing else: it validates the first-start
inputs, then publishes the private intent and `.env` (`cli/start_identity.py`).
It imports no runtime Settings, starts no process, creates no data plane and
dials only a joining runner's gateway. The first `ava start` provisions and
launches.
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

from base.host.env.registry import derived_env_keys, env_identity_keys
from base.host.net.predicates import is_loopback_host
from base.host.private_storage import ensure_private_dir
from base.native_process.os_platform import file_lock
from cli import start_intent
from cli.start_identity import (
    CAP_ARGS,
    IDENTITY_FIELDS,
    IdentityInput,
    capability_roles,
    pending_claim,
    prepare_identity,
    read_intent,
    resume_claim,
    stored_values,
)
from cli.unit_join import join_gateway

# Every flag a run can carry; an interrupted claim resumes from its recorded payload
# and takes none of them.
_FLAGS = (
    *IDENTITY_FIELDS,
    *CAP_ARGS.values(),
    "config_file",
    "ssl_cert_file",
    "db_capability",
)


def _config_values(args: argparse.Namespace, home: Path) -> tuple[dict[str, str], str | None]:
    if args.config_file is None:
        return {}, None
    from base.host.env.config_lite_table import FIELD_ALIASES

    path = Path(args.config_file).expanduser().resolve(strict=True)
    if path.is_relative_to(home):
        raise ValueError("first-start config file must be outside the home")
    content = path.read_bytes()
    values = dotenv_values(stream=io.StringIO(content.decode()), interpolate=False)
    remote_keys = {"AVA_DB_URL", "AVA_REDIS_URL", "AVA_RUNNER_DB_PASSWORD"}
    forbidden = (
        derived_env_keys() | env_identity_keys() | {"AVA_HOME", "AVA_REDIS_ADMIN_PASSWORD"}
    ) - remote_keys
    unknown = set(values) - set(FIELD_ALIASES.values()) - remote_keys
    if unknown or set(values) & forbidden or any(v is None for v in values.values()):
        raise ValueError("config file contains unknown, identity, resource, or valueless keys")
    remote = set(values) & remote_keys
    if remote and (remote != remote_keys or any(not values[key] for key in remote_keys)):
        raise ValueError("remote config file requires paired DB/Redis URLs and runner credential")
    if (home / ".env").exists():
        raise ValueError("config file is only accepted for a home with no `.env` yet")
    return {k: v for k, v in values.items() if v is not None}, hashlib.sha256(content).hexdigest()


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


def _first_time_examples() -> str:
    """What to run, from this checkout: the host's bare `ava` is linked by the first start."""
    cli = start_intent._checkout() / ".venv" / "bin" / "ava"
    return (
        "\nfirst-time examples (this checkout's CLI; a bare `ava` is linked by the first start):\n"
        "  # single box (owns data plane + runs agents):\n"
        f"  {cli} init --machine-name <name> --serve-gateway --serve-agent-runner\n"
        "  # gateway (data plane + HTTP gateway):\n"
        f"  {cli} init --machine-name <name> --serve-gateway --no-serve-agent-runner \\\n"
        "            --machine-host <this-host-addr> --gateway-url http://<this-host-addr>:8000\n"
        "  # agent-runner (joins a gateway; AVA_DB_CAPABILITY_KEY set first):\n"
        f"  {cli} init --machine-name <name> --serve-agent-runner --no-serve-gateway \\\n"
        "            --gateway-url <url> --machine-host <this-host-addr> --db-capability <bundle>\n"
        f"then `{cli} start`"
    )


def _inputs(args: argparse.Namespace, home: Path) -> IdentityInput:
    stored = stored_values(home)
    explicit = {cap: getattr(args, flag) for cap, flag in CAP_ARGS.items()}
    try:
        roles = capability_roles(stored, explicit)
    except ValueError as exc:
        raise ValueError(f"{exc}{_first_time_examples()}") from exc
    values: dict[str, str] = dict(stored)
    config, digest = _config_values(args, home)
    values.update(config)
    values["AVA_SERVICE_PATH"] = _service_path(values, home)
    _apply_identity_options(args, stored, values)
    if config.get("AVA_DB_URL"):
        _validate_remote_inputs(values, roles)
    if "gateway" not in roles:
        if not values.get("AVA_GATEWAY_URL"):
            raise ValueError("a remote unit's init requires --gateway-url")
        join_gateway(values, home, args.db_capability)
    elif args.db_capability is not None:
        raise ValueError(
            "--db-capability is for agent-runner units; a gateway unit keeps its own "
            "write-generation ledger"
        )
    return IdentityInput(home, start_intent._checkout(), roles, values, digest)


def _service_path(values: dict[str, str], home: Path) -> str:
    from base.sessions.env_forwarding import admit_service_path

    if "AVA_SERVICE_PATH" in values:
        return admit_service_path(values["AVA_SERVICE_PATH"])
    if (home / ".env").exists():
        raise ValueError("existing home requires an explicit AVA_SERVICE_PATH declaration")
    if "AVA_SERVICE_PATH" in os.environ:
        return admit_service_path(os.environ["AVA_SERVICE_PATH"])
    bin_name = "bin"
    venvs = [start_intent._checkout() / ".venv"]
    if sys.prefix != sys.base_prefix:
        venvs.append(Path(sys.prefix))
    if activated := os.environ.get("VIRTUAL_ENV"):
        venvs.append(Path(activated))
    return admit_service_path(
        os.environ.get("PATH", ""), excluded=tuple(venv / bin_name for venv in venvs)
    )


def _apply_identity_options(
    args: argparse.Namespace,
    stored: dict[str, str],
    values: dict[str, str],
) -> None:
    for field in IDENTITY_FIELDS:
        explicit = getattr(args, field)
        key = "AVA_" + field.upper()
        if explicit is not None:
            if key in stored and stored[key] != explicit:
                raise ValueError(f"init input conflicts with persisted {key}")
            values[key] = explicit
    if not values.get("AVA_MACHINE_NAME"):
        raise ValueError(f"init requires --machine-name{_first_time_examples()}")
    for key in ("AVA_MACHINE_NAME", "AVA_MACHINE_HOST"):
        if any(c in values.get(key, "") for c in "\r\n\x00"):
            raise ValueError(f"invalid {key}")
    if args.ssl_cert_file is not None:
        values.update(SSL_CERT_FILE=args.ssl_cert_file, REQUESTS_CA_BUNDLE=args.ssl_cert_file)
        os.environ["SSL_CERT_FILE"] = args.ssl_cert_file


def _given_flags(args: argparse.Namespace) -> list[str]:
    return ["--" + name.replace("_", "-") for name in _FLAGS if getattr(args, name) is not None]


def initialize_home(args: argparse.Namespace, home: Path) -> str:
    """Publish the home's identity; the verdict line for the operator.

    The caller holds the home's `start-intent.lock`.
    """
    if pending_claim(home) is not None:
        flags = _given_flags(args)
        if flags:
            raise ValueError(
                f"an earlier `ava init` was interrupted before it published this home; "
                f"re-run `ava init` without flags to finish it (given: {', '.join(flags)})"
            )
        resume_claim(home)
    else:
        prepare_identity(_inputs(args, home))
    intent = read_intent(home)
    assert intent is not None  # noqa: S101 — the claim was just published
    return f"initialized {home} (capabilities: {', '.join(intent['roles'])})"


def run_init(args: argparse.Namespace) -> int:
    try:
        home = start_intent._home()
        ensure_private_dir(home)
        with file_lock(home / "start-intent.lock", timeout_s=30):
            verdict = initialize_home(args, home)
        print(f"✓ {verdict}")
        print(f"next: run `{start_intent._checkout() / '.venv' / 'bin' / 'ava'} start`")
        return 0
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        print(f"ava init: {exc}", file=sys.stderr)
        return 1
