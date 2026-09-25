"""First/repeated/interrupted start preserves one home identity before effects."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from cli import start_identity as identity
from cli import start_intent
from shared import cluster


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> identity.IdentityInput:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(cluster, "_port_free", lambda _port: True)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
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
    assert env["AVA_CLUSTER_SECRET"] == ""
    assert env["AVA_RUNNER_DB_PASSWORD"]
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
    keys = (
        "AVA_CLUSTER_SECRET",
        "AVA_DB_ADMIN_PASSWORD",
        "AVA_REDIS_ADMIN_PASSWORD",
        "AVA_REDIS_PASSWORD",
        "AVA_RUNNER_DB_PASSWORD",
    )
    assert len({env[k] for k in keys}) == len(keys)


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
    start_intent.prepare_start(_args("--worktree"))
    env = dotenv_values(inputs.home / ".env")
    db_url = env["AVA_DB_URL"]
    assert db_url is not None and "foreign" not in db_url
    assert env["AVA_CLUSTER_SECRET"] == ""


@pytest.mark.parametrize(
    "key", ["AVA_HOME", "AVA_GATEWAY_PORT", "AVA_DB_URL", "AVA_MACHINE_NAME", "UNKNOWN_SETTING"]
)
def test_config_file_cannot_choose_identity(inputs: identity.IdentityInput, key: str) -> None:
    config = inputs.home.parent / "input.env"
    config.write_text(f"{key}=wrong\n")
    with pytest.raises(ValueError, match="config file"):
        start_intent._config_values(_args("--config-file", str(config)), inputs.home)


def test_runner_verifies_projection_before_persisting(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import bootstrap

    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_CLUSTER_REGISTRY", str(inputs.registry))
    monkeypatch.setenv("AVA_CLUSTER_SECRET", "runner-bearer")
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
            "AVA_DB_URL": "postgresql://owner@remote.invalid/db",
            "AVA_REDIS_URL": "redis://remote.invalid/0",
        },
    )
    args = _args(
        "--serve-agent-runner",
        "--no-serve-gateway",
        "--machine-name",
        "runner",
        "--machine-host",
        "runner.invalid",
        "--gateway-url",
        "https://gateway.invalid",
    )
    with pytest.raises(ValueError, match="projection"):
        start_intent.prepare_start(args)
    assert not (inputs.home / ".env").exists()
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_config",
        lambda *_a, **_k: {  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
            "AVA_DB_URL": "postgresql://ava_runner@remote.invalid/db",
            "AVA_REDIS_URL": "redis://remote.invalid/0",
        },
    )
    start_intent.prepare_start(args)
    env = dotenv_values(inputs.home / ".env")
    assert env["AVA_MACHINE_HOST"] == "runner.invalid"
    assert "AVA_DB_URL" not in env and "AVA_REDIS_URL" not in env
    assert not inputs.registry.exists()


def test_ready_phase_never_regresses_or_rewrites(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    identity.mark_phase(inputs.home, "ready")
    path = inputs.home / identity.INTENT_NAME
    before = path.read_bytes(), path.stat().st_mtime_ns
    identity.mark_phase(inputs.home, "provisioned")
    identity.mark_phase(inputs.home, "ready")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


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
    start_intent.prepare_start(args)
    first = (inputs.home / ".env").read_bytes()
    start_intent.prepare_start(args)
    assert (inputs.home / ".env").read_bytes() == first
    config.write_text("AVA_LLM_OVERRIDE=other:model\n")
    with pytest.raises(ValueError, match="differs"):
        start_intent.prepare_start(args)
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
    start_intent.prepare_start(args)
    env = dotenv_values(inputs.home / ".env")
    assert env["AVA_DB_URL"] == "postgresql://owner:provider@db.invalid:6543/app"
    assert env["AVA_REDIS_URL"] == "rediss://acl:provider@redis.invalid:6381/0"
    assert env["AVA_RUNNER_DB_PASSWORD"] == "external-runner"  # noqa: S105 — fixture credential
    assert "AVA_DB_ADMIN_PASSWORD" not in env
    assert "AVA_REDIS_ADMIN_PASSWORD" not in env
    before = (inputs.home / ".env").read_bytes()
    start_intent.prepare_start(args)
    assert (inputs.home / ".env").read_bytes() == before


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "this-host", "192.0.2.1"])
def test_remote_input_refuses_local_or_mixed_ownership(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    args = _remote_config(inputs, monkeypatch, host=host)
    with pytest.raises(ValueError, match="foreign"):
        start_intent.prepare_start(args)
    assert not inputs.registry.exists()


@pytest.mark.parametrize("query", ["?hostaddr=127.0.0.1", "?host=", "?port=", "?dbname=other"])
def test_remote_input_cannot_redirect_libpq_identity(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    args = _remote_config(inputs, monkeypatch, query=query)
    with pytest.raises(ValueError, match="redirect"):
        start_intent.prepare_start(args)
    assert not inputs.registry.exists()


def test_public_start_holds_home_lock_through_runtime_start(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands
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

    monkeypatch.setattr(cli.commands, "cmd_start", runtime)
    assert start_intent.run_start(_args("--worktree")) == 0
