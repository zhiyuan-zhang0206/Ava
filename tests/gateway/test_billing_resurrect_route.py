"""Billing batch-recovery route (task #3919) — preview/execute body plumbing."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import app


def _stub_run(captured: dict[str, Any]) -> Any:
    async def _run(*, execute: bool, pool: object) -> dict[str, Any]:
        captured["execute"] = execute
        return {
            "mode": "execute" if execute else "dry_run",
            "outcome": "executed" if execute else "preview",
            "refusal_reason": None,
            "balance": {
                "ok": True,
                "detail": "ok",
                "threshold": 1.0,
                "total": 9.5,
                "currency": "CNY",
            },
            "agents": [],
            "halted_alive": [{"agent_id": 7, "machine": "m", "streak": 2}],
        }

    return _run


class TestBillingResurrectRoute:
    def test_defaults_to_a_read_only_preview(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import billing_recovery

        captured: dict[str, Any] = {}
        monkeypatch.setattr(billing_recovery, "run_billing_recovery", _stub_run(captured))

        with TestClient(app) as client:
            resp = client.post("/api/agents/resurrect-billing", json={})

        assert resp.status_code == 200
        assert captured["execute"] is False
        assert resp.json()["outcome"] == "preview"
        assert resp.json()["halted_alive"] == [{"agent_id": 7, "machine": "m", "streak": 2}]

    def test_execute_flag_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops import billing_recovery

        captured: dict[str, Any] = {}
        monkeypatch.setattr(billing_recovery, "run_billing_recovery", _stub_run(captured))

        with TestClient(app) as client:
            resp = client.post("/api/agents/resurrect-billing", json={"execute": True})

        assert resp.status_code == 200
        assert captured["execute"] is True
        assert resp.json()["outcome"] == "executed"
