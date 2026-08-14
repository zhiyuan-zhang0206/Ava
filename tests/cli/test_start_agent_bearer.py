"""`scripts/start_agent.py` presents the cluster secret on its bootstrap spawn.

`POST /api/agents` is an authenticated gateway route; the bootstrap-spawn script
must carry `Authorization: Bearer <cluster secret>` or a multi-host gateway 401s
it. Load the script by path (it is not a package module) and assert the header.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_start_agent():
    path = Path(__file__).resolve().parents[2] / "scripts" / "start_agent.py"
    spec = importlib.util.spec_from_file_location("start_agent_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_start_agent_posts_with_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_start_agent()
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

        @staticmethod
        def json() -> dict[str, int]:
            return {"id": 7}

    def _fake_post(url: str, *, json: dict, headers: dict) -> _Resp:
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(mod, "gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(mod, "gateway_auth_headers", lambda: {"Authorization": "Bearer sekret"})
    monkeypatch.setattr(mod, "dial_post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]

    mod.main()

    assert captured["url"] == "http://gw:8000/api/agents"
    # The Bearer header is what a multi-host gateway requires (PR#100 pattern).
    assert captured["headers"] == {"Authorization": "Bearer sekret"}
