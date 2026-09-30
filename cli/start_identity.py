"""Durable unit identity for the single start lifecycle; no runtime settings or effects.

The private intent precedes `.env` publication and is the home's record of itself:
a gateway unit's ports (the fixed table) and data-plane host live in its `record`
(`base.cluster.record`), and no other file on the host lists this cluster. A
home without an intent that already holds a data plane cannot be born again
over it; explicit reattachment is required.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from dotenv import dotenv_values

from base import cluster
from base.host.atomic_io import write_text_atomic
from base.host.env.dotenv_file import upsert_env
from base.host.private_storage import ensure_private_dir, ensure_private_file
from cli.start_runtime import StartRuntime

INTENT_NAME = cluster.INTENT_NAME
_CAPS = ("gateway", "agent-runner", "observability-station")
_PHASES = ("claiming", "configured", "provisioned", "ready")


@dataclass(frozen=True)
class IdentityInput:
    home: Path
    checkout: Path
    roles: frozenset[str]
    values: dict[str, str]
    config_digest: str | None = None
    runtime: StartRuntime | None = None


def _write(path: Path, data: dict[str, Any]) -> None:
    ensure_private_file(path)
    write_text_atomic(path, json.dumps(data, sort_keys=True) + "\n", mode=0o600, sync_parent=True)


def read_intent(home: Path) -> dict[str, Any] | None:
    path = home / INTENT_NAME
    if not path.exists():
        return None
    ensure_private_file(path)
    data = json.loads(path.read_text())
    required = {"version", "home", "checkout", "roles", "config_digest", "phase", "record", "env"}
    if not isinstance(data, dict):
        raise TypeError("invalid start identity shape")
    data = cast("dict[str, Any]", data)
    if set(data) != required:
        raise RuntimeError(
            f"invalid start identity shape: unexpected keys {sorted(set(data) - required)}, "
            f"missing keys {sorted(required - set(data))}"
        )
    if data["version"] != 1 or data["home"] != str(home):
        raise RuntimeError("start identity version/home mismatch")
    if data["phase"] not in _PHASES:
        raise RuntimeError("unknown start identity phase")
    _validate_intent_fields(data, home)
    return data


def _validate_intent_fields(data: dict[str, Any], home: Path) -> None:
    if not isinstance(data["checkout"], str) or not Path(data["checkout"]).is_absolute():
        raise RuntimeError("invalid start checkout")
    raw_roles = data["roles"]
    if not isinstance(raw_roles, list):
        raise TypeError("invalid start capabilities")
    roles = cast("list[Any]", raw_roles)
    if not roles or any(r not in _CAPS for r in roles) or roles != sorted(set(roles)):
        raise RuntimeError("invalid start capabilities")
    _validate_intent_environment(data)
    _validate_intent_record(data["record"], home, gateway="gateway" in roles)


def _validate_intent_environment(data: dict[str, Any]) -> None:
    raw = data["env"]
    if not isinstance(raw, dict):
        raise TypeError("invalid start environment")
    env = cast("dict[Any, Any]", raw)
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        raise RuntimeError("invalid start environment")
    digest = data["config_digest"]
    if digest is not None and (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise RuntimeError("invalid first-start config digest")


def _validate_intent_record(raw: Any, home: Path, *, gateway: bool) -> None:
    if not gateway:
        if raw is not None:
            raise RuntimeError("remote unit cannot claim a gateway reservation")
        return
    if not isinstance(raw, dict):
        raise TypeError("invalid start reservation")
    rec = cast("dict[str, Any]", raw)
    if rec.get("gateway_home") != str(home):
        raise RuntimeError("start reservation belongs to another home")
    raw_ports = rec.get("ports")
    if not isinstance(raw_ports, dict):
        raise TypeError("invalid start port reservation")
    ports = cast("dict[str, Any]", raw_ports)
    if set(ports) != set(cluster.FIXED_PORTS):
        raise RuntimeError(
            "invalid start port reservation: slots differ from the fixed port table "
            f"(unexpected {sorted(set(ports) - set(cluster.FIXED_PORTS))}, "
            f"missing {sorted(set(cluster.FIXED_PORTS) - set(ports))})"
        )
    if any(type(p) is not int or not 1 <= p <= 65535 for p in ports.values()):
        raise RuntimeError("invalid start port number")
    if len(set(ports.values())) != len(ports):
        raise RuntimeError("duplicate start port reservation")


def mark_phase(home: Path, phase: str) -> None:
    if phase not in _PHASES:
        raise ValueError("unknown start phase")
    data = read_intent(home)
    if data is not None and _PHASES.index(phase) > _PHASES.index(data["phase"]):
        data["phase"] = phase
        _write(home / INTENT_NAME, data)


def needs_provision(home: Path) -> bool:
    data = read_intent(home)
    return data is not None and data["phase"] == "configured"


def _new_record(inputs: IdentityInput) -> cluster.ClusterRecord:
    ports = cluster.new_home_ports()
    if not all(cluster.port_free(p) for p in cast("dict[str, int]", ports).values()):
        raise RuntimeError("the home's ports are already occupied")
    return cluster.ClusterRecord(
        ports=ports,
        gateway_home=str(inputs.home),
        created_at=datetime.now(UTC).isoformat(),
        data_plane_host=inputs.values.get("AVA_DATA_PLANE_HOST", ""),
    )


def _gateway_values(rec: cluster.ClusterRecord, inputs: IdentityInput) -> dict[str, str]:
    vals = inputs.values
    secret = vals.get("AVA_CLUSTER_SECRET", "")
    if not secret and inputs.roles != frozenset({"gateway", "agent-runner"}):
        secret = secrets.token_urlsafe(32)
    db, redis = cluster.per_cluster_base_urls(rec)
    derived = cluster.derive_env(
        rec,
        base_db_url=db,
        base_redis_url=redis,
        cluster_secret=secret,
        # Redis always authenticates, whatever the bearer (decisions/
        # 2026-09-26-internal-data-plane-always-authenticated.md). Postgres
        # needs no minted password: its owner is NOLOGIN and the first start
        # mints write generation 0 into the home's database authority store.
        redis_admin_password=secrets.token_urlsafe(32),
        redis_password=secrets.token_urlsafe(32),
        pgbouncer_enabled=vals.get("AVA_PGBOUNCER_ENABLED", "true").lower()
        in {"1", "true", "yes", "on"},
    )
    if "AVA_DB_URL" in vals:
        # Provider credentials are explicit inputs, never locally fabricated.
        for key in ("AVA_REDIS_ADMIN_PASSWORD", "AVA_REDIS_PASSWORD"):
            derived.pop(key)
    # Explicit remote-managed URLs are never rewritten into local URLs.
    derived.update(vals)
    return derived


def _validate_existing(
    inputs: IdentityInput, env: dict[str, str | None], rec: cluster.ClusterRecord | None
) -> None:
    for key, value in inputs.values.items():
        if key in env and env[key] != value:
            raise RuntimeError(
                f"start cannot change persisted identity/configuration {key}; configure it explicitly"
            )
    if "gateway" in inputs.roles:
        if rec is None or not env.get("AVA_DB_URL") or not env.get("AVA_REDIS_URL"):
            raise RuntimeError(
                "existing home has no recorded identity (start-intent.json) or is incomplete; "
                "explicit reattachment is required"
            )
    elif not env.get("AVA_GATEWAY_URL"):
        raise RuntimeError("existing remote unit has no gateway identity")


def _publish_claim(inputs: IdentityInput, data: dict[str, Any]) -> None:
    if data["phase"] == "claiming":
        if data["record"] is not None:
            # A gateway's logical backups are encrypted under a passphrase minted
            # here, independent of its cluster secret (an empty one included).
            from services.gateway_side.backup import passphrase

            passphrase.ensure_minted(inputs.home)
        upsert_env(inputs.home / ".env", data["env"])
        data["phase"] = "configured"
        # `.env` now holds the payload, credentials included; the intent keeps
        # no copy that a later rotation would leave stale.
        data["env"] = {}
        _write(inputs.home / INTENT_NAME, data)


def _validate_repeat(inputs: IdentityInput, data: dict[str, Any]) -> None:
    if data["roles"] != sorted(inputs.roles):
        raise RuntimeError("start capability set differs from the persisted identity")


def prepare_identity(inputs: IdentityInput) -> None:
    """Persist one complete identity before any process or database effect.

    The caller holds the home's `start-intent.lock`.
    """
    ensure_private_dir(inputs.home)
    if (inputs.home / "destroy-intent.json").exists():
        raise RuntimeError("home is being destroyed or detached; explicit reattachment required")
    data = read_intent(inputs.home)
    if data is not None:
        _validate_repeat(inputs, data)
    _prepare_reserved_identity(inputs, data)


def _prepare_reserved_identity(inputs: IdentityInput, data: dict[str, Any] | None) -> None:
    if data is not None:
        _publish_claim(inputs, data)
        _validate_existing(
            inputs,
            dotenv_values(inputs.home / ".env"),
            cluster.ClusterRecord(**data["record"]) if data["record"] else None,
        )
        return
    env = dotenv_values(inputs.home / ".env")
    if env.get("AVA_DB_URL") or env.get("AVA_GATEWAY_URL"):
        _validate_existing(inputs, env, None)
        return
    if any(
        (inputs.home / name).exists()
        for name in (
            "pg",
            "redis",
            "pgbouncer",
            "deploy-state.json",
            "run/deploy-pause-owner.json",
        )
    ):
        raise RuntimeError(
            "existing resource state has no initialization authority; refusing fresh start"
        )
    _create_claim(inputs)


def _create_claim(inputs: IdentityInput) -> None:
    values = dict(inputs.values)
    for cap in _CAPS:
        values["AVA_MACHINE_SERVE_" + cap.upper().replace("-", "_")] = str(
            cap in inputs.roles
        ).lower()
    rec = _new_record(inputs) if "gateway" in inputs.roles else None
    if rec is not None:
        values = _gateway_values(
            rec, IdentityInput(inputs.home, inputs.checkout, inputs.roles, values)
        )
    data = {
        "version": 1,
        "home": str(inputs.home),
        "checkout": str(inputs.checkout),
        "roles": sorted(inputs.roles),
        "config_digest": inputs.config_digest,
        "phase": "claiming",
        "record": asdict(rec) if rec else None,
        "env": values,
    }
    _write(inputs.home / INTENT_NAME, data)
    _publish_claim(inputs, data)
