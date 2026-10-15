from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from tests.fixtures.gateway_config import gateway_test_client


class TestRefreshPath:
    def test_refresh_returns_head_sha(
        self, monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity
    ) -> None:
        import base.deploy.git.memory_repo as _memory_repo

        calls = {"n": 0}

        def _fake_pull_main(*, machine: MachineIdentity) -> str:
            assert machine is machine_identity
            calls["n"] += 1
            return "abc1234"

        monkeypatch.setattr(_memory_repo, "pull_main", _fake_pull_main)

        with gateway_test_client(machine=machine_identity) as client:
            resp = client.post("/api/memory/refresh")
        assert resp.status_code == 200
        assert resp.json()["head"] == "abc1234"
        assert calls["n"] == 1


class TestRefreshOffEventLoop:
    def test_refresh_runs_pull_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity
    ) -> None:
        import asyncio as _asyncio
        import threading

        import base.deploy.git.memory_repo as _memory_repo

        real_to_thread = cast("Callable[..., object]", _asyncio.to_thread)
        handoffs: list[tuple[object, int]] = []
        pull_threads: list[int] = []

        def _fake_pull_main(*, machine: MachineIdentity) -> str:
            assert machine is machine_identity
            pull_threads.append(threading.get_ident())
            return "abc1234"

        def _probe_to_thread(fn: Callable[..., object], *args: object, **kwargs: object) -> object:
            handoffs.append((fn, threading.get_ident()))
            return real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(_memory_repo, "pull_main", _fake_pull_main)
        monkeypatch.setattr(_asyncio, "to_thread", _probe_to_thread)

        with gateway_test_client(machine=machine_identity) as client:
            resp = client.post("/api/memory/refresh")
        assert resp.status_code == 200
        assert resp.json()["head"] == "abc1234"
        assert pull_threads, "pull_main never ran"
        assert handoffs, "the handler never routed pull_main through asyncio.to_thread"
        assert pull_threads[0] != handoffs[0][1]
