"""Durable unit identity for the single start lifecycle; no runtime settings or effects.

The private intent precedes registry/env publication. Only its claiming phase may
complete a missing reservation; a configured home whose reservation disappears
requires explicit reattachment. No failure frees a reservation behind live effects.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from dotenv import dotenv_values

from shared import cluster
from shared.atomic_io import write_text_atomic
from shared.envfile import upsert_env
from shared.private_storage import ensure_private_dir, ensure_private_file

INTENT_NAME = "start-intent.json"
_CAPS = ("gateway", "agent-runner", "observability-station")
_PHASES = ("claiming", "configured", "provisioned", "ready")


@dataclass(frozen=True)
class IdentityInput:
    home: Path
    registry: Path
    checkout: Path
    worktree: bool
    roles: frozenset[str]
    values: dict[str, str]
    config_digest: str | None = None


def _write(path: Path, data: dict[str, Any]) -> None:
    ensure_private_file(path)
    write_text_atomic(path, json.dumps(data, sort_keys=True) + "\n", mode=0o600, sync_parent=True)


def read_intent(home: Path) -> dict[str, Any] | None:
    path = home / INTENT_NAME
    if not path.exists():
        return None
    ensure_private_file(path)
    data = json.loads(path.read_text())
    required = {
        "version",
        "home",
        "checkout",
        "worktree",
        "roles",
        "config_digest",
        "phase",
        "record",
        "env",
    }
    if not isinstance(data, dict):
        raise TypeError("invalid start identity shape")
    data = cast("dict[str, Any]", data)
    if set(data) != required:
        raise RuntimeError("invalid start identity shape")
    if data["version"] != 1 or data["home"] != str(home):
        raise RuntimeError("start identity version/home mismatch")
    if data["phase"] not in _PHASES:
        raise RuntimeError("unknown start identity phase")
    _validate_intent_fields(data, home)
    return data


def _validate_intent_fields(data: dict[str, Any], home: Path) -> None:
    if not isinstance(data["checkout"], str) or not Path(data["checkout"]).is_absolute():
        raise RuntimeError("invalid start checkout")
    if type(data["worktree"]) is not bool:
        raise RuntimeError("invalid start worktree flag")
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
    if set(ports) != set(cluster.LEGACY_AVA_PORTS):
        raise RuntimeError("invalid start port reservation")
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


def _new_record(
    inputs: IdentityInput, records: dict[str, cluster.ClusterRecord]
) -> cluster.ClusterRecord:
    if cluster.is_default_home(inputs.home):
        ports = cast("cluster.ClusterPorts", cluster.LEGACY_AVA_PORTS.copy())
        wanted = set(cast("dict[str, int]", ports).values())
        if any(wanted.intersection(other.ports.values()) for other in records.values()) or not all(
            cluster._port_free(p) for p in wanted
        ):
            raise RuntimeError("default home's ports are already reserved or occupied")
    else:
        ports = cluster.allocate_ports(
            {min(cast("dict[str, int]", r.ports).values()) for r in records.values()}
        )
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
    passwords = [secrets.token_urlsafe(32) if secret else "" for _ in range(3)]
    db, redis = cluster.per_cluster_base_urls(rec)
    derived = cluster.derive_env(
        rec,
        base_db_url=db,
        base_redis_url=redis,
        cluster_secret=secret,
        db_admin_password=passwords[0],
        redis_admin_password=passwords[1],
        redis_password=passwords[2],
        pgbouncer_enabled=vals.get("AVA_PGBOUNCER_ENABLED", "true").lower()
        in {"1", "true", "yes", "on"},
    )
    if "AVA_DB_URL" in vals:
        # Provider credentials are explicit inputs, never locally fabricated.
        for key in ("AVA_DB_ADMIN_PASSWORD", "AVA_REDIS_ADMIN_PASSWORD", "AVA_REDIS_PASSWORD"):
            derived.pop(key)
    else:
        # The runner projection requires a durable credential even when local
        # trust authentication means the loopback listener does not challenge it.
        derived["AVA_RUNNER_DB_PASSWORD"] = secrets.token_urlsafe(32)
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
                "unregistered or incomplete existing home; explicit reattachment is required"
            )
        from cli.preflight import _port_block_conflicts

        conflicts = _port_block_conflicts(asdict(rec), env)
        if conflicts:
            raise RuntimeError(
                "home configuration conflicts with its port reservation: " + "; ".join(conflicts)
            )
    elif not env.get("AVA_GATEWAY_URL"):
        raise RuntimeError("existing remote unit has no gateway identity")


def _publish_claim(
    inputs: IdentityInput, data: dict[str, Any], records: dict[str, cluster.ClusterRecord]
) -> None:
    rec_data = data["record"]
    if rec_data is not None:
        rec = cluster.ClusterRecord(**rec_data)
        existing = records.get(str(inputs.home))
        if existing is not None and asdict(existing) != rec_data:
            raise RuntimeError("start intent and registry disagree")
        if existing is None:
            if data["phase"] != "claiming":
                raise RuntimeError(
                    "configured home lost its registry reservation; explicit reattachment required"
                )
            wanted = set(rec.ports.values())
            if any(wanted.intersection(other.ports.values()) for other in records.values()):
                raise RuntimeError("interrupted start reservation is now owned by another home")
            cluster.save_record_locked(rec, path=inputs.registry)
    if data["phase"] == "claiming":
        upsert_env(inputs.home / ".env", data["env"])
        if data["worktree"]:
            pointer = inputs.checkout / ".ava_home"
            write_text_atomic(pointer, str(inputs.home) + "\n", sync_parent=True)
        data["phase"] = "configured"
        _write(inputs.home / INTENT_NAME, data)


def _validate_repeat(inputs: IdentityInput, data: dict[str, Any]) -> None:
    if data["roles"] != sorted(inputs.roles):
        raise RuntimeError("start capability set differs from the persisted identity")
    if data["worktree"] and data["checkout"] != str(inputs.checkout):
        raise RuntimeError("worktree home belongs to another checkout")
    if inputs.worktree and not data["worktree"]:
        raise RuntimeError("home was initialized without worktree identity")


def prepare_identity(inputs: IdentityInput) -> None:
    """Persist one complete identity before any process or database effect.

    Caller holds the home start lock. Registry allocation and the recoverable
    claiming record are serialized together under the shared host registry lock.
    """
    ensure_private_dir(inputs.home)
    if (inputs.home / "destroy-intent.json").exists():
        raise RuntimeError("home is being destroyed or detached; explicit reattachment required")
    with cluster.registry_lock(path=inputs.registry):
        records = cluster.load_registry(path=inputs.registry)
        data = read_intent(inputs.home)
        if data is not None:
            _validate_repeat(inputs, data)
            _publish_claim(inputs, data, records)
            _validate_existing(
                inputs,
                dotenv_values(inputs.home / ".env"),
                records.get(str(inputs.home))
                or (cluster.ClusterRecord(**data["record"]) if data["record"] else None),
            )
            return
        env = dotenv_values(inputs.home / ".env")
        rec = records.get(str(inputs.home))
        if rec is not None or env.get("AVA_DB_URL") or env.get("AVA_GATEWAY_URL"):
            _validate_existing(inputs, env, rec)
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
        _create_claim(inputs, records)


def _create_claim(inputs: IdentityInput, records: dict[str, cluster.ClusterRecord]) -> None:
    values = dict(inputs.values)
    for cap in _CAPS:
        values["AVA_MACHINE_SERVE_" + cap.upper().replace("-", "_")] = str(
            cap in inputs.roles
        ).lower()
    rec = _new_record(inputs, records) if "gateway" in inputs.roles else None
    if rec is not None:
        values = _gateway_values(
            rec,
            IdentityInput(
                inputs.home,
                inputs.registry,
                inputs.checkout,
                inputs.worktree,
                inputs.roles,
                values,
            ),
        )
    data = {
        "version": 1,
        "home": str(inputs.home),
        "checkout": str(inputs.checkout),
        "worktree": inputs.worktree,
        "roles": sorted(inputs.roles),
        "config_digest": inputs.config_digest,
        "phase": "claiming",
        "record": asdict(rec) if rec else None,
        "env": values,
    }
    _write(inputs.home / INTENT_NAME, data)
    _publish_claim(inputs, data, records)
