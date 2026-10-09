"""Durable unit identity for `ava init` and `ava start`; no runtime settings or effects.

The private intent precedes `.env` publication and is the home's record of itself:
a gateway unit's ports (the fixed table) and data-plane host live in its `record`
(`base.cluster.record`), and no other file on the host lists this cluster. `ava init`
writes it once (`prepare_identity`, `resume_claim`); `ava start` only admits a home
that carries it (`require_initialized`). A home without an intent that already
holds a data plane cannot be born again over it; explicit reattachment is required.
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
from base.native_process.os_platform import is_windows

INTENT_NAME = cluster.INTENT_NAME
_CAPS = ("gateway", "agent-runner", "observability-station")
_PHASES = ("claiming", "configured", "provisioned", "ready")
_DESTROY_INTENT = "destroy-intent.json"
# The capability flag each capability is declared with, and the identity fields a
# home records beside it: `ava init`'s inputs, and what a home declares for itself.
CAP_ARGS = {
    "gateway": "serve_gateway",
    "agent-runner": "serve_agent_runner",
    "observability-station": "serve_observability_station",
}
IDENTITY_FIELDS = (
    "machine_name",
    "machine_host",
    "machine_description",
    "memory_remote",
    "gateway_url",
)


@dataclass(frozen=True)
class IdentityInput:
    home: Path
    checkout: Path
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
        # Redis always authenticates, whatever the bearer (docs/decisions/
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


def _publish_claim(home: Path, data: dict[str, Any]) -> None:
    if data["phase"] == "claiming":
        if data["record"] is not None:
            # A gateway's logical backups are encrypted under a passphrase minted
            # here, independent of its cluster secret (an empty one included).
            from services.backup.artifact import passphrase

            passphrase.ensure_minted(home)
        upsert_env(home / ".env", data["env"])
        data["phase"] = "configured"
        # `.env` now holds the payload, credentials included; the intent keeps
        # no copy that a later rotation would leave stale.
        data["env"] = {}
        _write(home / INTENT_NAME, data)


def _refuse_detached(home: Path) -> None:
    if (home / _DESTROY_INTENT).exists():
        raise RuntimeError("home is being destroyed or detached; explicit reattachment required")


def pending_claim(home: Path) -> dict[str, Any] | None:
    """The home's interrupted claim; None for a home with no intent.

    Refuses a detached home and a home `ava init` already finished: initialization
    runs once, and changing an identity means destroying the cluster first.
    """
    _refuse_detached(home)
    data = read_intent(home)
    if data is None:
        _require_unclaimed_state(home)
    elif data["phase"] != "claiming":
        raise RuntimeError(
            f"home {home} is already initialized (phase {data['phase']}); `ava init` runs "
            "once per home. Use `ava start`; to change this home's identity, run "
            "`ava cluster destroy` first"
        )
    return data


def _require_unclaimed_state(home: Path) -> None:
    """A home without an intent may carry nothing that says whose it is."""
    env = dotenv_values(home / ".env")
    if env.get("AVA_DB_URL") or env.get("AVA_GATEWAY_URL"):
        raise RuntimeError(
            "existing home has no recorded identity (start-intent.json) or is incomplete; "
            "explicit reattachment is required"
        )
    if _holds_resources(home):
        raise RuntimeError(
            "existing resource state has no initialization authority; refusing fresh start"
        )


def resume_claim(home: Path) -> None:
    """Finish an interrupted claim from the payload its intent recorded.

    The caller holds the home's `start-intent.lock`. The recorded ports and
    credentials are published as they were minted; the input flags of the
    interrupted run are not asked for again.
    """
    data = pending_claim(home)
    if data is None:
        raise RuntimeError("no interrupted claim to resume")
    _publish_claim(home, data)


def prepare_identity(inputs: IdentityInput) -> None:
    """Persist one complete identity before any process or database effect.

    The caller holds the home's `start-intent.lock`. A home that already has an
    intent is never claimed again here: an interrupted claim resumes through
    `resume_claim`, an initialized home is refused.
    """
    ensure_private_dir(inputs.home)
    if pending_claim(inputs.home) is not None:
        raise RuntimeError(
            "an interrupted claim exists; re-run `ava init` without flags to finish it"
        )
    _create_claim(inputs)


def _holds_resources(home: Path) -> bool:
    return any(
        (home / name).exists()
        for name in (
            "pg",
            "redis",
            "pgbouncer",
            "deploy-state.json",
            "run/deploy-pause-owner.json",
        )
    )


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
    _publish_claim(inputs.home, data)


# --- what a home declares for itself, and the admission `ava start` gives it -------------


def stored_values(home: Path) -> dict[str, str]:
    """What the home already declares: its `.env`, the only place a home keeps its
    identity fields and capability flags."""
    return {k: v for k, v in dotenv_values(home / ".env").items() if v is not None}


def capability_value(cap: str, stored: dict[str, str], *, explicit: bool | None) -> bool | None:
    key = "AVA_MACHINE_" + CAP_ARGS[cap].upper()
    raw = stored.get(key)
    if raw is not None and raw.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError(f"invalid capability value {key}")
    prior = raw.lower() in {"true", "1", "yes", "on"} if raw is not None else None
    if explicit is not None and prior is not None and explicit != prior:
        raise ValueError(f"init cannot change persisted capability {cap}")
    return prior if prior is not None else explicit


def capability_roles(stored: dict[str, str], explicit: dict[str, bool | None]) -> frozenset[str]:
    """The capability set the stored declarations and the explicit flags give.

    `explicit` maps a capability name to the flag `ava init` was given for it; a
    capability it omits was not given one.
    """
    roles = {cap for cap in CAP_ARGS if capability_value(cap, stored, explicit=explicit.get(cap))}
    if not roles:
        raise ValueError(
            "no capability declared: `ava init` takes --serve-gateway, --serve-agent-runner "
            "and/or --serve-observability-station"
        )
    if is_windows():
        raise ValueError("native Windows is unsupported; use WSL2 or a POSIX host")
    return frozenset(roles)


@dataclass(frozen=True)
class InitializedHome:
    """What `ava start` admitted: the capability set and the intent it comes from."""

    roles: frozenset[str]
    intent: dict[str, Any]


def require_initialized(home: Path) -> InitializedHome:
    """Admit `home` for `ava start`, or raise why it cannot start. No side effects.

    Settings-free. The checks are those a start has always made of an existing
    home; the first-start inputs are no longer among them (`ava init` takes them).
    """
    _refuse_detached(home)
    intent = read_intent(home)
    if intent is None:
        _require_unclaimed_state(home)
        raise RuntimeError(
            f"home {home} is not initialized: run `ava init` first (it takes the machine name, "
            "the capabilities and, for a runner, the gateway; see `ava init --help`)"
        )
    env = dotenv_values(home / ".env")
    if intent["phase"] == "claiming":
        raise RuntimeError(
            f"home {home} was only partly initialized: an earlier `ava init` was interrupted "
            "before it published this home's configuration. Re-run `ava init` (no flags) to finish it"
        )
    roles = frozenset(intent["roles"])
    if "gateway" in roles:
        if not env.get("AVA_DB_URL") or not env.get("AVA_REDIS_URL"):
            raise RuntimeError(
                "existing home has no recorded identity or is incomplete; "
                "explicit reattachment is required"
            )
    elif not env.get("AVA_GATEWAY_URL"):
        raise RuntimeError("existing remote unit has no gateway identity")
    _require_host_declarations(home, roles, env)
    return InitializedHome(roles, intent)


def _require_host_declarations(
    home: Path, roles: frozenset[str], env: dict[str, str | None]
) -> None:
    from base.sessions.env_forwarding import admit_service_path

    if "gateway" not in roles and env.get("AVA_CLUSTER_SECRET") is not None:
        raise RuntimeError(
            f"this remote unit's home ({home}) records the human cluster secret; a remote "
            "unit authenticates with its capability's machine API token and never holds "
            "it. Remove AVA_CLUSTER_SECRET from its .env"
        )
    declared = env.get("AVA_SERVICE_PATH")
    if declared is None:
        raise RuntimeError("existing home requires an explicit AVA_SERVICE_PATH declaration")
    admit_service_path(declared)
