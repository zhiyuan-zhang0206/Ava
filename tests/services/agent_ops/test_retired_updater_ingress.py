"""Retired updater requests refuse before daemon, database or transport effects."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import pytest

from ops import cluster_rpc
from ops.rpc_schemas import is_op_kind
from services.agent_ops import daemon

RETIRED = (
    "cluster_update",
    "cluster_fetch",
    "cluster_prepare_facts",
    "cluster_prepare_dispatch",
    "cluster_bootstrap_hop",
    "cluster_bootstrap_recovery_read",
    "cluster_normal_continue",
)


def _effect(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("retired request reached an effect boundary")


@pytest.mark.parametrize("kind", RETIRED)
def test_retired_wire_arm_has_no_registration_or_sync_handler(kind: str) -> None:
    assert not is_op_kind(kind)
    assert daemon._dispatch_sync(kind, {}) == ("failed", {"error": f"unknown kind: {kind!r}"})


@pytest.mark.parametrize("kind", RETIRED)
@pytest.mark.parametrize("key", (None, "recorded-old-success"))
async def test_retired_request_refuses_before_admission_or_dedupe(
    kind: str, key: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "_dispatch_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(daemon.maintenance_activity, "admission", _effect)
    monkeypatch.setattr(daemon, "_dispatch_idempotent", _effect)
    monkeypatch.setattr(daemon, "_dispatch", _effect)
    status, body, content_type = await daemon._ops_route(
        json.dumps({"kind": kind, "payload": {}, "idempotency_key": key}).encode()
    )
    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body) == {"status": "failed", "result": {"error": f"unknown kind: {kind!r}"}}


@pytest.mark.parametrize("kind", RETIRED)
async def test_retired_direct_dispatch_refuses_before_workers_or_database(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "_db_pool", object())
    monkeypatch.setattr(daemon, "_run_arm", _effect)
    monkeypatch.setattr(daemon, "_dispatch_idempotent_pass", _effect)
    expected = ("failed", {"error": f"unknown kind: {kind!r}"})
    assert await daemon._dispatch(kind, {}) == expected
    assert await daemon._dispatch_idempotent(kind, {}, "old-key", daemon._db_pool) == expected


@pytest.mark.parametrize("kind", RETIRED)
async def test_retired_client_refuses_before_lookup_key_or_network(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cluster_rpc, "lookup_machine_url", _effect)
    monkeypatch.setattr(cluster_rpc, "_default_idempotency_key", _effect)
    monkeypatch.setattr(cluster_rpc, "_dispatch_once", _effect)
    with pytest.raises(ValueError, match="unknown op kind"):
        await cluster_rpc.dispatch_to_machine("runner", kind, {})


@pytest.mark.parametrize(
    "argv",
    (
        ["--bootstrap-observation", "/private/retired.json"],
        ["--bootstrap-observation=/private/retired.json"],
        ["--bootstrap", "/private/retired.json"],
        ["--unknown-option"],
        ["unexpected-positional"],
    ),
)
def test_unknown_daemon_argv_refuses_before_ordinary_imports(argv: list[str]) -> None:
    root = Path(__file__).resolve().parents[3]
    script = """import importlib.abc, runpy, sys
sys.path.insert(0, sys.argv[1])
sys.argv = ['services.agent_ops.daemon', *sys.argv[2:]]
class DenyEffects(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(('shared.config', 'services.agent_ops._boot',
                                'services._pidfile', 'ops')):
            raise AssertionError('ordinary startup imported before argv refusal: ' + fullname)
sys.meta_path.insert(0, DenyEffects())
runpy.run_module('services.agent_ops.daemon', run_name='__main__')
"""
    result = subprocess.run(  # noqa: S603 — repository module, fixed guard and isolated interpreter
        [sys.executable, "-I", "-c", script, str(root), *argv],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "unrecognized arguments:" in result.stderr
    assert "ordinary startup imported" not in result.stderr


def test_unknown_main_argv_refuses_before_eager_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.config.ensure_eager", _effect)
    monkeypatch.setattr(daemon, "init_gateway_process", _effect)
    with pytest.raises(SystemExit) as failure:
        daemon.main(argv=["--bootstrap-observation", "/private/retired.json"])
    assert failure.value.code == 2
