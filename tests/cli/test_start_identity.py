"""First/repeated/interrupted start preserves one home identity before effects."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from dotenv import dotenv_values

from cli import start_identity as identity
from cli import start_intent
from shared import cluster
from shared.platform import file_lock
from tests.lifecycle._start_identity import prepare_start_identity


@pytest.fixture(autouse=True)
def _isolate_start_environment() -> Iterator[None]:
    """Real start clears derived keys; that process-local effect ends with its test."""
    with patch.dict(os.environ):
        yield


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> identity.IdentityInput:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(cluster, "port_free", lambda _port: True)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    return identity.IdentityInput(
        tmp_path / "home",
        tmp_path / "registry.json",
        checkout,
        True,
        frozenset({"gateway", "agent-runner"}),
        {"AVA_MACHINE_NAME": "preview"},
    )


def test_fresh_identity_committed_before_native_work(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    data = identity.read_intent(inputs.home)
    assert data is not None and data["phase"] == "configured"
    rec = cluster.load_registry(path=inputs.registry)[str(inputs.home)]
    env = dotenv_values(inputs.home / ".env")
    assert "pgbouncer" in rec.ports
    db_url = env["AVA_DB_URL"]
    assert db_url is not None and db_url.endswith(f":{rec.ports['pgbouncer']}/ava")
    # The database endpoint is credential-free: the owner is NOLOGIN and the
    # first start mints write generation 0; no DB password is ever minted here.
    assert urlsplit(db_url).password is None
    assert env["AVA_CLUSTER_SECRET"] == ""
    assert "AVA_DB_ADMIN_PASSWORD" not in env
    assert "AVA_RUNNER_DB_PASSWORD" not in env
    # Redis always authenticates: a no-secret single box is still born with
    # independent Redis admin and runtime passwords, the runtime one in its URL.
    redis_admin, redis_runtime = env["AVA_REDIS_ADMIN_PASSWORD"], env["AVA_REDIS_PASSWORD"]
    assert redis_admin and redis_runtime and redis_admin != redis_runtime
    redis_url = urlsplit(env["AVA_REDIS_URL"] or "")
    assert (redis_url.username, redis_url.password) == ("ava", redis_runtime)
    assert (inputs.checkout / ".ava_home").read_text().strip() == str(inputs.home)
    assert not (inputs.home / "pg").exists()


def test_repeat_preserves_identity_credentials_and_bytes(inputs: identity.IdentityInput) -> None:
    inputs = replace(inputs, roles=frozenset({"gateway"}))
    identity.prepare_identity(inputs)
    paths = [inputs.home / ".env", inputs.home / identity.INTENT_NAME, inputs.registry]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    identity.prepare_identity(inputs)
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before
    env = dotenv_values(paths[0])
    keys = ("AVA_CLUSTER_SECRET", "AVA_REDIS_ADMIN_PASSWORD", "AVA_REDIS_PASSWORD")
    assert len({env[k] for k in keys}) == len(keys)
    assert all(env[k] for k in keys)


def test_a_gateway_birth_pins_a_minted_logical_backup_passphrase(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logical backups are encrypted under a passphrase the birth mints and pins
    (0600), independent of the cluster secret: an empty-secret single box gets
    a random one, never the public sha256(""). An interrupted birth keeps it."""
    from services.gateway_side.backup import passphrase

    upsert = identity.upsert_env

    def crash(*_a: object, **_k: object) -> None:
        raise OSError("power loss")

    monkeypatch.setattr(identity, "upsert_env", crash)
    with pytest.raises(OSError, match="power loss"):
        identity.prepare_identity(inputs)
    minted = passphrase.pinned(inputs.home)
    assert minted is not None and minted != passphrase.LEGACY_EMPTY_SECRET_PASSPHRASE
    assert passphrase.pin_path(inputs.home).stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(identity, "upsert_env", upsert)
    identity.prepare_identity(inputs)
    assert dotenv_values(inputs.home / ".env")["AVA_CLUSTER_SECRET"] == ""
    assert passphrase.resolve(inputs.home) == minted


def test_a_remote_unit_birth_pins_no_backup_passphrase(inputs: identity.IdentityInput) -> None:
    """Only a gateway runs logical backups; a joining agent-runner holds no key."""
    from services.gateway_side.backup import passphrase

    runner = replace(
        inputs,
        roles=frozenset({"agent-runner"}),
        values={**inputs.values, "AVA_GATEWAY_URL": "http://10.0.0.7:8000"},
    )
    identity.prepare_identity(runner)
    assert passphrase.pinned(runner.home) is None


def test_a_published_claim_keeps_no_copy_of_its_credentials(
    inputs: identity.IdentityInput,
) -> None:
    """The intent carries the birth's `.env` payload only while it is claiming,
    so an interrupted birth resumes the same credentials. Once `.env` holds
    them the intent drops the payload: no stale copy of the human secret or the
    Redis passwords outlives a rotation there."""
    inputs = replace(inputs, roles=frozenset({"gateway"}))
    identity.prepare_identity(inputs)
    env = dotenv_values(inputs.home / ".env")
    credentials = [
        env[key] for key in ("AVA_CLUSTER_SECRET", "AVA_REDIS_ADMIN_PASSWORD", "AVA_REDIS_PASSWORD")
    ]
    assert all(credentials)
    data = identity.read_intent(inputs.home)
    assert data is not None and data["phase"] == "configured" and data["env"] == {}
    raw = (inputs.home / identity.INTENT_NAME).read_text()
    assert not [value for value in credentials if value and value in raw]


def test_stale_inputs_cannot_rebind_checkout_across_private_registries(
    inputs: identity.IdentityInput,
) -> None:
    other = replace(
        inputs,
        home=inputs.home.with_name("other"),
        registry=inputs.registry.with_name("other.json"),
    )
    identity.prepare_identity(inputs)
    with pytest.raises(RuntimeError, match="bound to another home"):
        identity.prepare_identity(other)
    assert (inputs.checkout / ".ava_home").read_text().strip() == str(inputs.home)
    assert identity.read_intent(other.home) is None
    assert not other.registry.exists()


@pytest.mark.parametrize(
    ("location", "name"),
    [
        ("checkout", ".ava_home.json"),
        ("checkout", ".ava_home"),
        ("home", "start-intent.json"),
        ("home", "start-intent.other"),
        ("home", "destroy-intent.json"),
        ("home", ".env"),
        ("home", "registry.lock"),
    ],
)
def test_registry_cannot_alias_lifecycle_data_or_lock(
    inputs: identity.IdentityInput, location: str, name: str
) -> None:
    registry = (inputs.checkout if location == "checkout" else inputs.home) / name
    with pytest.raises(ValueError, match="must not alias lifecycle"):
        identity.prepare_identity(replace(inputs, registry=registry))
    assert not inputs.home.exists()


def test_checkout_lock_serializes_independent_home_publication(
    inputs: identity.IdentityInput,
) -> None:
    entered = Event()

    def start() -> None:
        entered.set()
        identity.prepare_identity(inputs)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with file_lock(inputs.checkout / ".ava_home.lock", timeout_s=3):
            pending = executor.submit(start)
            assert entered.wait(3)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.2)
            assert identity.read_intent(inputs.home) is None
        pending.result(timeout=5)
    assert (inputs.checkout / ".ava_home").read_text().strip() == str(inputs.home)


def test_retirement_excludes_rebinding_until_pointer_is_removed(
    inputs: identity.IdentityInput,
) -> None:
    identity.prepare_identity(inputs)
    other = replace(
        inputs,
        home=inputs.home.with_name("other"),
        registry=inputs.registry.with_name("other.json"),
    )
    entered = Event()

    def start() -> None:
        entered.set()
        identity.prepare_identity(other)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with file_lock(inputs.checkout / ".ava_home.lock", timeout_s=3):
            pending = executor.submit(start)
            assert entered.wait(3)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.2)
            identity._retire_checkout_pointer(inputs.checkout, inputs.home)
        pending.result(timeout=5)
    assert (inputs.checkout / ".ava_home").read_text().strip() == str(other.home)


def test_crash_after_intent_before_registry_resumes_same_claim(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = cluster.save_record_locked
    monkeypatch.setattr(
        cluster,
        "save_record_locked",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("power loss")),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    with pytest.raises(OSError, match="power loss"):
        identity.prepare_identity(inputs)
    pending = identity.read_intent(inputs.home)
    assert pending is not None and pending["phase"] == "claiming"
    monkeypatch.setattr(cluster, "save_record_locked", save)
    identity.prepare_identity(inputs)
    assert json.loads(inputs.registry.read_text())[str(inputs.home)] == pending["record"]
    assert dict(dotenv_values(inputs.home / ".env")) == pending["env"]


def test_bare_repeat_preserves_recorded_checkout_binding(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    paths = [inputs.home / ".env", inputs.home / identity.INTENT_NAME, inputs.registry]
    before = [path.read_bytes() for path in paths]
    identity.prepare_identity(replace(inputs, worktree=False))
    with pytest.raises(RuntimeError, match="another checkout"):
        identity.prepare_identity(
            replace(inputs, worktree=False, checkout=inputs.checkout.parent / "other")
        )
    assert [path.read_bytes() for path in paths] == before


def test_worktree_flag_cannot_change_existing_identity_mode(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(replace(inputs, worktree=False))
    with pytest.raises(RuntimeError, match="without worktree identity"):
        identity.prepare_identity(inputs)


def test_crash_after_env_before_pointer_recovers_own_home(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = identity.write_text_atomic

    def crash(path: Path, data: str, **kwargs: Any) -> None:
        if path.name == ".ava_home":
            raise OSError("power loss")
        write(path, data, **kwargs)

    monkeypatch.setattr(identity, "write_text_atomic", crash)
    with pytest.raises(OSError, match="power loss"):
        identity.prepare_identity(inputs)
    before = (inputs.home / ".env").read_bytes()
    monkeypatch.setattr(identity, "write_text_atomic", write)
    identity.prepare_identity(inputs)
    assert (inputs.home / ".env").read_bytes() == before
    persisted = identity.read_intent(inputs.home)
    assert persisted is not None and persisted["phase"] == "configured"


def test_configured_home_missing_registry_is_not_reborn(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    inputs.registry.unlink()
    with pytest.raises(RuntimeError, match="reattachment"):
        identity.prepare_identity(inputs)
    assert not inputs.registry.exists()


def test_existing_data_without_intent_refuses(inputs: identity.IdentityInput) -> None:
    (inputs.home / "pg").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="no initialization authority"):
        identity.prepare_identity(inputs)
    assert not inputs.registry.exists()


def test_claim_cannot_steal_reallocated_ports(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = identity.upsert_env
    monkeypatch.setattr(
        identity,
        "upsert_env",
        lambda *_a: (_ for _ in ()).throw(OSError("interrupted")),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    with pytest.raises(OSError):
        identity.prepare_identity(inputs)
    raw = json.loads(inputs.registry.read_text())
    rec = raw.pop(str(inputs.home))
    rec["gateway_home"] = str(inputs.home.parent / "other")
    inputs.registry.write_text(json.dumps({rec["gateway_home"]: rec}))
    monkeypatch.setattr(identity, "upsert_env", write)
    with pytest.raises(RuntimeError, match="another home"):
        identity.prepare_identity(inputs)


def _args(*extra: str):
    from cli.parsers import build_parser

    return build_parser().parse_args(["start", *extra])


def test_worktree_start_uses_explicit_home_not_ambient_projection(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))
    monkeypatch.setenv("AVA_DB_URL", "postgresql://foreign@foreign.invalid/production")
    monkeypatch.setenv("AVA_CLUSTER_SECRET", "foreign-secret")
    prepare_start_identity(_args("--worktree"))
    env = dotenv_values(inputs.home / ".env")
    db_url = env["AVA_DB_URL"]
    assert db_url is not None and "foreign" not in db_url
    assert env["AVA_CLUSTER_SECRET"] == ""


def test_interrupted_start_keeps_admitted_tool_path(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))
    monkeypatch.delenv("AVA_SERVICE_PATH", raising=False)
    activated = inputs.checkout.parent / "other-venv"
    tools = str(inputs.checkout.parent / "tools")
    bin_name = "Scripts" if start_intent.IS_WINDOWS else "bin"
    monkeypatch.setenv("VIRTUAL_ENV", str(activated))
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            (str(activated / bin_name), str(inputs.checkout / ".venv" / bin_name), tools)
        ),
    )
    save = cluster.save_record_locked

    def crash(*_args: object, **_kwargs: object) -> None:
        raise OSError("power loss")

    monkeypatch.setattr(cluster, "save_record_locked", crash)
    with pytest.raises(OSError, match="power loss"):
        prepare_start_identity(_args("--worktree"))
    pending = identity.read_intent(inputs.home)
    assert pending is not None and pending["phase"] == "claiming"
    assert pending["env"]["AVA_SERVICE_PATH"] == tools
    monkeypatch.setattr(cluster, "save_record_locked", save)
    monkeypatch.setenv("PATH", str(inputs.checkout.parent / "different-tools"))
    prepare_start_identity(_args("--worktree"))
    assert dotenv_values(inputs.home / ".env")["AVA_SERVICE_PATH"] == tools


def test_existing_home_cannot_recapture_caller_tool_path(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity.prepare_identity(inputs)
    monkeypatch.setenv("AVA_SERVICE_PATH", str(inputs.checkout.parent / "unadmitted"))
    with pytest.raises(ValueError, match="explicit AVA_SERVICE_PATH"):
        start_intent._service_path(start_intent._stored(inputs.home), inputs.home)


def test_lossy_tool_path_is_rejected_before_identity_publication(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))
    monkeypatch.delenv("AVA_SERVICE_PATH", raising=False)
    monkeypatch.setenv("PATH", str(inputs.checkout.parent / "tools #1"))
    with pytest.raises(ValueError, match="round-trip literally"):
        prepare_start_identity(_args("--worktree"))
    assert identity.read_intent(inputs.home) is None
    assert not inputs.registry.exists()


@pytest.mark.parametrize(
    "key", ["AVA_HOME", "AVA_GATEWAY_PORT", "AVA_DB_URL", "AVA_MACHINE_NAME", "UNKNOWN_SETTING"]
)
def test_config_file_cannot_choose_identity(inputs: identity.IdentityInput, key: str) -> None:
    config = inputs.home.parent / "input.env"
    config.write_text(f"{key}=wrong\n")
    with pytest.raises(ValueError, match="config file"):
        start_intent._config_values(_args("--config-file", str(config)), inputs.home)


_RUNNER_ENDPOINT = "postgresql://ava@remote.invalid/db"


_FETCH_BEARERS: list[object] = []


def _runner_start(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> Any:
    """A remote runner's first start against a gateway serving the endpoint only."""
    from shared import bootstrap

    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))
    # A remote unit joins without the human secret: its capability authenticates.
    monkeypatch.delenv("AVA_CLUSTER_SECRET", raising=False)

    def served(*_a: object, **kwargs: object) -> dict[str, str]:
        _FETCH_BEARERS.append(kwargs.get("bearer"))
        return {"AVA_DB_URL": _RUNNER_ENDPOINT, "AVA_REDIS_URL": "redis://remote.invalid/0"}

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", served)
    return _args(
        "--serve-agent-runner",
        "--no-serve-gateway",
        "--machine-name",
        "runner",
        "--machine-host",
        "runner.invalid",
        "--gateway-url",
        "https://gateway.invalid",
        *extra,
    )


def test_runner_without_a_capability_refuses_before_persisting(
    inputs: identity.IdentityInput,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seed_write_generation: Any,
) -> None:
    """Bootstrap serves no login, so a runner's first start needs its unit
    capability; without one it refuses before any identity is written."""
    from shared.cluster.authority import unit

    args = _runner_start(inputs, monkeypatch)
    with pytest.raises(ValueError, match="holds no database capability"):
        prepare_start_identity(args)
    assert not (inputs.home / ".env").exists()
    # An installed capability (a bundle the operator carried earlier) admits it.
    gateway = tmp_path / "gateway"
    gateway.mkdir(mode=0o700)
    seed_write_generation(gateway)
    home = inputs.home.resolve()
    issued = unit.issue_bundle(
        gateway.resolve(),
        unit=unit.UnitIdentity(machine="runner", home=str(home)),
        endpoint=_RUNNER_ENDPOINT,
        cluster_secret="gateway-human-secret-" + "g" * 32,
        ttl_s=60,
    )
    unit.install_bundle(
        home,
        unit.open_bundle(issued.envelope, issued.transport_key),
        machine="runner",
        served_endpoint=_RUNNER_ENDPOINT,
        probe=lambda _dsn: None,
    )
    _FETCH_BEARERS.clear()
    prepare_start_identity(args)
    env = dotenv_values(inputs.home / ".env")
    assert env["AVA_MACHINE_HOST"] == "runner.invalid"
    assert "AVA_DB_URL" not in env and "AVA_REDIS_URL" not in env
    # The fetch authenticated with the capability's API token; no bearer persisted.
    installed = unit.require_unit_capability(home)
    assert installed.api is not None and [installed.api.token] == _FETCH_BEARERS
    assert "AVA_CLUSTER_SECRET" not in env
    assert not inputs.registry.exists()


def test_capability_bundle_needs_its_transport_key(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "unit.bundle"
    bundle.write_text("{}")
    monkeypatch.delenv("AVA_DB_CAPABILITY_KEY", raising=False)
    args = _runner_start(inputs, monkeypatch, "--db-capability", str(bundle))
    with pytest.raises(ValueError, match="AVA_DB_CAPABILITY_KEY"):
        prepare_start_identity(args)
    assert bundle.exists()
    assert not (inputs.home / ".env").exists()


def test_gateway_start_refuses_a_capability_bundle(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))
    with pytest.raises(ValueError, match="agent-runner units"):
        prepare_start_identity(
            _args("--worktree", "--db-capability", str(tmp_path / "unit.bundle"))
        )
    assert identity.read_intent(inputs.home) is None


def test_ready_phase_never_regresses_or_rewrites(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    identity.mark_phase(inputs.home, "ready")
    path = inputs.home / identity.INTENT_NAME
    before = path.read_bytes(), path.stat().st_mtime_ns
    identity.mark_phase(inputs.home, "provisioned")
    identity.mark_phase(inputs.home, "ready")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_reservation_carries_the_release_coordinator_port(inputs: identity.IdentityInput) -> None:
    """The fleet coordinator listener's port is part of every gateway reservation;
    an intent recorded without it (a record from before the key) refuses."""
    identity.prepare_identity(inputs)
    rec = cluster.load_registry(path=inputs.registry)[str(inputs.home)]
    ports: dict[str, int] = dict(rec.ports)  # pyright: ignore[reportAssignmentType]
    assert ports["coordinator"] == ports["gateway"] + 20
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    del data["record"]["ports"]["coordinator"]
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="invalid start port reservation"):
        identity.read_intent(inputs.home)


def test_corrupt_foreign_reservation_never_publishes(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    data["phase"] = "claiming"
    data["record"]["gateway_home"] = str(inputs.home.parent / "foreign")
    path.write_text(json.dumps(data))
    before = inputs.registry.read_bytes()
    with pytest.raises(RuntimeError, match="another home"):
        identity.prepare_identity(inputs)
    assert inputs.registry.read_bytes() == before


def test_config_retry_keeps_first_snapshot_and_rejects_changed_input(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))
    config = inputs.home.parent / "config.env"
    config.write_text("AVA_LLM_OVERRIDE=fixture:model\n")
    args = _args("--worktree", "--config-file", str(config))
    prepare_start_identity(args)
    first = (inputs.home / ".env").read_bytes()
    prepare_start_identity(args)
    assert (inputs.home / ".env").read_bytes() == first
    config.write_text("AVA_LLM_OVERRIDE=other:model\n")
    with pytest.raises(ValueError, match="differs"):
        prepare_start_identity(args)
    assert (inputs.home / ".env").read_bytes() == first


def _remote_config(
    inputs: identity.IdentityInput,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str = "db.invalid",
    query: str = "",
):
    import socket

    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setattr(
        start_intent,
        "_local_addresses",
        lambda _machine: {"this-host", "192.0.2.1"},  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.55", 0))],  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))
    config = inputs.home.parent / "remote.env"
    config.write_text(
        f"AVA_DB_URL=postgresql://owner:provider@{host}:6543/app{query}\nAVA_REDIS_URL=rediss://acl:provider@redis.invalid:6381/0\nAVA_RUNNER_DB_PASSWORD=external-runner\n"
    )
    return _args("--worktree", "--config-file", str(config))


def test_remote_start_preserves_provider_urls_and_runner_credential(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _remote_config(inputs, monkeypatch)
    prepare_start_identity(args)
    env = dotenv_values(inputs.home / ".env")
    assert env["AVA_DB_URL"] == "postgresql://owner:provider@db.invalid:6543/app"
    assert env["AVA_REDIS_URL"] == "rediss://acl:provider@redis.invalid:6381/0"
    assert env["AVA_RUNNER_DB_PASSWORD"] == "external-runner"  # noqa: S105 — fixture credential
    assert "AVA_DB_ADMIN_PASSWORD" not in env
    assert "AVA_REDIS_ADMIN_PASSWORD" not in env
    before = (inputs.home / ".env").read_bytes()
    prepare_start_identity(args)
    assert (inputs.home / ".env").read_bytes() == before


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "this-host", "192.0.2.1"])
def test_remote_input_refuses_local_or_mixed_ownership(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    args = _remote_config(inputs, monkeypatch, host=host)
    with pytest.raises(ValueError, match="foreign"):
        prepare_start_identity(args)
    assert not inputs.registry.exists()


@pytest.mark.parametrize("query", ["?hostaddr=127.0.0.1", "?host=", "?port=", "?dbname=other"])
def test_remote_input_cannot_redirect_libpq_identity(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    args = _remote_config(inputs, monkeypatch, query=query)
    with pytest.raises(ValueError, match="redirect"):
        prepare_start_identity(args)
    assert not inputs.registry.exists()


def test_public_start_holds_home_lock_through_runtime_start(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:

    from shared.platform import LockTimeoutError, file_lock

    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))

    def runtime(**_kw: object) -> int:
        with (
            pytest.raises(LockTimeoutError),
            file_lock(inputs.home / "start-intent.lock", timeout_s=0.05),
        ):
            pytest.fail("another lifecycle operation entered during start")
        return 0

    monkeypatch.setattr("cli.commands.start.cmd_start", runtime)
    assert start_intent.run_start(_args("--worktree")) == 0


def test_start_parser_rejects_retired_updater_telemetry() -> None:
    with pytest.raises(SystemExit) as error:
        _args("--updater-telemetry")
    assert error.value.code == 2


@pytest.mark.parametrize("result", [0, 1, 75])
def test_public_start_publishes_boot_pid_only_after_complete_success(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, result: int
) -> None:

    from cli.commands import root_driver

    calls: list[str] = []
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))

    def runtime(**_kw: object) -> int:
        calls.append("complete wrapped start")
        return result

    monkeypatch.setattr("cli.commands.start.cmd_start", runtime)
    monkeypatch.setattr(root_driver, "complete_boot_start", lambda: calls.append("publish PID"))
    assert start_intent.run_start(_args("--worktree")) == result
    assert calls == ["complete wrapped start"] + (["publish PID"] if result == 0 else [])


def test_failed_boot_publication_clears_serving_and_refuses_success(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:

    from cli.commands import root_driver
    from shared.deploy.lifecycle import start_serving

    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))
    calls: list[str] = []

    def ready(**_kw: object) -> int:
        return 0

    monkeypatch.setattr("cli.commands.start.cmd_start", ready)
    monkeypatch.setattr(start_serving, "clear_serving", lambda: calls.append("cleared"))

    def fail() -> None:
        raise RuntimeError("manager refused native custody")

    monkeypatch.setattr(root_driver, "complete_boot_start", fail)
    assert start_intent.run_start(_args("--worktree")) == 1
    assert calls == ["cleared"]
