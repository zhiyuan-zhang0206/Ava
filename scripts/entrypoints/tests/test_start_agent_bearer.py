"""`scripts/entrypoints/agent.py` presents the cluster secret on its bootstrap spawn.

The keyed spawn route is authenticated; the bootstrap-spawn script must carry
`Authorization: Bearer <cluster secret>` or a multi-host gateway 401s it, plus the
keyed-admission headers.
"""

from __future__ import annotations

from typing import cast

import pytest

from scripts.entrypoints import agent as mod


def test_start_agent_posts_with_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert captured["url"] == "http://gw:8000/api/keyed/v1/agents"
    # The Bearer header is what a multi-host gateway requires (PR#100 pattern).
    headers = cast(dict[str, str], captured["headers"])
    assert headers["Authorization"] == "Bearer sekret"
    assert headers["Idempotency-Scope"] == "principal-v1"
    assert 1 <= len(headers["Idempotency-Key"]) <= 128
