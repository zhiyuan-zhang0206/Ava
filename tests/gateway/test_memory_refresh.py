"""`POST /api/memory/refresh` endpoint unit tests — trigger gateway checkout
fast-forward to origin/main, return HEAD sha after pull."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gateway.app import app


class TestRefreshPath:
    """refresh calls shared.memory_repo.pull_main(), passing the returned sha through to the caller."""

    def test_refresh_returns_head_sha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stub pull_main → verify 200 + head returned, and pull_main called once."""
        import shared.memory_repo as _memory_repo

        calls = {"n": 0}

        def _fake_pull_main() -> str:
            calls["n"] += 1
            return "abc1234"

        monkeypatch.setattr(_memory_repo, "pull_main", _fake_pull_main)

        with TestClient(app) as client:
            resp = client.post("/api/memory/refresh")
        assert resp.status_code == 200
        assert resp.json()["head"] == "abc1234"
        assert calls["n"] == 1
