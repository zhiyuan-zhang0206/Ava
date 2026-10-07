"""Config api cases: get explicit self is host view not."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from base.cluster.machine import machine_name
from base.config import settings
from base.host.env import audit, runtime_config
from gateway.app import app
from gateway.routers import config as config_router
from gateway.routers.tests.test_config_api import REMOTE, _seed_machine
from gateway.routers.tests.test_config_api import (
    _clean_overrides as _clean_overrides,
)
from gateway.routers.tests.test_config_api import (
    _stub_browser_capability as _stub_browser_capability,
)
from ops import cluster_rpc as _cluster_rpc
from ops.rpc_schemas import ConfigAuditReadResult


def test_get_explicit_self_is_host_view_not_cluster(_clean_overrides: Path) -> None:
    """GET `?machine=<self>` is a host view: raw_overrides is the remote_writable
    host set, so a writable-but-not-remote_writable field
    (cross_machine_transfer_backend) that the Cluster view exposes is ABSENT here —
    the explicit self selection no longer collapses into the Cluster view."""
    runtime_config.write_fields({"cross_machine_transfer_backend": "none"}, set())
    with TestClient(app) as client:
        cluster = client.get("/api/config").json()["raw_overrides"]
        host = client.get(f"/api/config?machine={machine_name()}").json()["raw_overrides"]
    assert (
        cluster.get("cross_machine_transfer_backend") == "none"
    )  # writable -> Cluster view exposes it
    assert (
        "cross_machine_transfer_backend" not in host
    )  # not remote_writable -> host view excludes it


def test_put_remote_rejects_cluster_key(_clean_overrides: Path) -> None:
    """PUT ?machine=<remote> carrying a cluster key -> 400 (cluster config is
    machine-independent)."""
    _seed_machine(REMOTE)
    with TestClient(app) as client:
        resp = client.put(f"/api/config?machine={REMOTE}", json={"llm_model": "x"})
    assert resp.status_code == 400, resp.text
    assert "machine-independent" in resp.json()["detail"]


def test_put_remote_host_field_dispatches_config_write(
    monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path
) -> None:
    """PUT ?machine=<remote> with a host remote_writable key -> dispatches a
    config_write carrying the overrides + gateway-stamped actor/trace, and
    returns the stubbed ConfigWriteResult."""
    _seed_machine(REMOTE)
    stub_result = {
        "machine": REMOTE,
        "results": {"ops_concurrency": {"ok": True, "reason": None}},
        "applied": True,
        "restart_required": ["ops"],
    }
    enqueue = AsyncMock(return_value=stub_result)
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.put(f"/api/config?machine={REMOTE}", json={"ops_concurrency": 12})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["results"]["ops_concurrency"]["ok"] is True
    assert body["restart_required"] == ["ops"]

    assert enqueue.await_count == 1
    assert enqueue.await_args is not None
    kwargs = enqueue.await_args.kwargs
    assert kwargs["target_machine"] == REMOTE
    assert kwargs["kind"] == "config_write"
    payload = kwargs["payload"]
    assert payload["overrides"] == {"ops_concurrency": 12}
    assert payload["local"] is False
    assert payload["actor"] is None  # unauthenticated test client: no verified principal
    assert isinstance(payload["trace_id"], str) and payload["trace_id"]


def test_put_remote_rejects_writable_non_remote_host_field(
    monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path
) -> None:
    """A writable-but-not-remote_writable host field (cross_machine_transfer_backend) is
    rejected at the gate on a machine-addressed PUT — 400 before any dispatch,
    rather than a 200 applied=False from the host-side op. Locks the shared
    field_editable gate: remote host edits require remote_writable."""
    _seed_machine(REMOTE)
    dispatched = AsyncMock()
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", dispatched)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/config?machine={REMOTE}", json={"cross_machine_transfer_backend": "none"}
        )
    assert resp.status_code == 400, resp.text
    assert "unknown or read-only" in resp.json()["detail"]
    dispatched.assert_not_awaited()


def test_put_remote_rejects_oss_credential_fields(
    monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path
) -> None:
    """The OSS credential path is host-local editable only: a machine-addressed PUT
    400s at the gate (remote_writable=False), so a remote admin can never point a
    machine at arbitrary credential files. Locks the security boundary the
    writable flip must not loosen."""
    _seed_machine(REMOTE)
    dispatched = AsyncMock()
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", dispatched)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/config?machine={REMOTE}",
            json={
                "backup_offsite_credentials_file": str(
                    _clean_overrides / "attacker-credentials.json"
                )
            },
        )
        assert resp.status_code == 400, resp.text
        assert "unknown or read-only" in resp.json()["detail"]
    dispatched.assert_not_awaited()


def test_put_remote_offline_503(monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path) -> None:
    """PUT ?machine=<known-but-offline>: config_write times out -> 503."""
    _seed_machine(REMOTE)
    enqueue = AsyncMock(side_effect=_cluster_rpc.ClusterOpUnreachable("no ack"))
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.put(f"/api/config?machine={REMOTE}", json={"ops_concurrency": 12})
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


def test_put_remote_failed_503(monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path) -> None:
    """PUT ?machine=<known>: the remote config_write fails (row marked failed) -> 503."""
    _seed_machine(REMOTE)
    enqueue = AsyncMock(side_effect=_cluster_rpc.ClusterOpFailed({"error": "boom"}))
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.put(f"/api/config?machine={REMOTE}", json={"ops_concurrency": 12})
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


def test_put_remote_unknown_machine_404(_clean_overrides: Path) -> None:
    """PUT ?machine=<unknown> -> 404 before any dispatch."""
    with TestClient(app) as client:
        resp = client.put("/api/config?machine=ghost-host", json={"ops_concurrency": 1})
    assert resp.status_code == 404, resp.text
    assert "unknown machine" in resp.json()["detail"]


def test_self_machine_name_is_known_without_a_row() -> None:
    """Sanity: the resolved self machine_name is treated as known even with no
    machines row (the GET/PUT self path must not 404)."""
    # machine_name() resolves to this process's identity; _assert_machine_known
    # short-circuits it. A bare self GET already exercises this, but assert the
    # name is non-empty so the short-circuit is meaningful.
    assert machine_name()


def test_request_actor_reads_only_middleware_state() -> None:
    """The write-audit actor comes from verified request state, not caller JSON."""
    from types import SimpleNamespace

    from gateway.routers.config import _request_actor

    anon = cast("Any", SimpleNamespace(state=SimpleNamespace()))
    assert _request_actor(anon) == (None, None)

    authed = cast(
        "Any",
        SimpleNamespace(
            state=SimpleNamespace(
                source_verified_by="user_session",
                auth_principal=SimpleNamespace(subject="administrator"),
                trace_id="trace-7",
            ),
        ),
    )
    assert _request_actor(authed) == ("user_session:administrator", "trace-7")

    bearer = cast(
        "Any",
        SimpleNamespace(
            state=SimpleNamespace(source_verified_by="cluster_bearer", auth_principal=None)
        ),
    )
    assert _request_actor(bearer) == ("cluster_bearer", None)


def test_put_records_write_audit_with_value_diff(_clean_overrides: Path) -> None:
    """A cluster PUT lands an audit record carrying the non-sensitive value diff."""
    with TestClient(app) as client:
        resp = client.put("/api/config", json={"llm_model": "audit-model-1"})
    assert resp.status_code == 200, resp.text
    records = [
        json.loads(line)
        for line in (_clean_overrides / ".env.audit.jsonl").read_text().splitlines()
    ]
    latest = records[-1]
    entries = cast("list[dict[str, object]]", latest["changed"])
    changed = {str(entry["alias"]): entry for entry in entries}
    assert changed["AVA_MODEL"]["new"] == "audit-model-1"
    assert "actor" in latest


def test_get_config_audit_last_is_bounded() -> None:
    """`last` is a 1..200 query param; out of range is a 422, never a clamped read."""
    with TestClient(app) as client:
        assert client.get("/api/config/audit?last=0").status_code == 422
        assert client.get("/api/config/audit?last=201").status_code == 422


def test_get_config_audit_default_last_comes_from_display_config(
    _clean_overrides: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitted `last` is ``settings.display.config_audit_default_last``
    (``AVA_CONFIG_AUDIT_DEFAULT_LAST``); the literal 20 is only that field's
    default, not a hard-coded cap."""
    from base.agents import MachineNotRegistered

    def _known(_target: str) -> None:
        return None

    monkeypatch.setattr(config_router, "_assert_machine_known", _known)

    def _unregistered(_db: object, _name: str) -> list[str]:
        raise MachineNotRegistered(_name)

    monkeypatch.setattr("base.cluster.machines.lookup_role", _unregistered)

    env_path = runtime_config.env_file_path()
    env_path.write_text("AVA_MODEL=audit-one\n")
    audit.record_env_write(
        env_path,
        {"AVA_MODEL"},
        set(),
        site="test-one",
        changes=[{"alias": "AVA_MODEL", "old": "old", "new": "audit-one"}],
    )
    audit.record_env_write(
        env_path,
        {"AVA_MODEL"},
        set(),
        site="test-two",
        changes=[{"alias": "AVA_MODEL", "old": "audit-one", "new": "audit-two"}],
    )

    monkeypatch.setattr(settings.display, "config_audit_default_last", 1)
    with TestClient(app) as client:
        resp = client.get("/api/config/audit")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["records"]) == 1


def test_get_config_audit_reads_own_records(
    _clean_overrides: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self audit read returns this box's newest records, tagged with its machine."""
    from base.agents import MachineNotRegistered

    def _known(_target: str) -> None:
        return None

    monkeypatch.setattr(config_router, "_assert_machine_known", _known)

    def _unregistered(_db: object, _name: str) -> list[str]:
        raise MachineNotRegistered(_name)

    monkeypatch.setattr("base.cluster.machines.lookup_role", _unregistered)
    env_path = runtime_config.env_file_path()
    env_path.write_text("AVA_MODEL=audit-self\n")
    audit.record_env_write(
        env_path,
        {"AVA_MODEL"},
        set(),
        site="test-self",
        changes=[{"alias": "AVA_MODEL", "old": "old", "new": "audit-self"}],
    )

    with TestClient(app) as client:
        resp = client.get("/api/config/audit")

    assert resp.status_code == 200, resp.text
    records = resp.json()["records"]
    assert records[-1]["site"] == "test-self"
    assert records[-1]["machine"] == machine_name()
    changed = records[-1]["changed"]
    assert changed[0]["alias"] == "AVA_MODEL"
    assert changed[0]["new"] == "audit-self"


@pytest.mark.asyncio
async def test_get_config_audit_all_merges_runners_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """machine=all fans out to the runners + this box, merges ts-desc, caps at last."""
    stamp = {"m1": "01", "m2": "02", machine_name(): "00"}
    called: list[str] = []

    async def _fake_dispatch(target: str, last: int) -> ConfigAuditReadResult:
        called.append(target)
        return ConfigAuditReadResult(
            machine=target,
            records=[{"ts": f"2026-09-16T00:0{stamp[target]}:00+00:00", "site": f"s-{target}"}],
        )

    monkeypatch.setattr(config_router, "_dispatch_config_audit_read", _fake_dispatch)

    def _runners(_db: object) -> list[tuple[str, str | None]]:
        return [("m1", None), ("m2", None)]

    monkeypatch.setattr("base.cluster.machines.list_agent_runners", _runners)

    with TestClient(app) as client:
        resp = client.get("/api/config/audit?machine=all&last=2")

    assert resp.status_code == 200, resp.text
    records = resp.json()["records"]
    assert [record["machine"] for record in records] == ["m2", "m1"]
    assert machine_name() in called
