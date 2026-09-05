"""Tests for ava.agents.presets — the SDK surface for config presets.

Driven through TestClient(app) against the real test DB. The presets router
is a plain CRUD surface with no spawn/lifecycle dependency, so the test setup
is simpler than the agents-SDK tests: just wire the TestClient as the SDK's
httpx client inside the lifespan context.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _sdk_via_testclient(monkeypatch: pytest.MonkeyPatch):
    """Wire the SDK's httpx client to an in-process FastAPI TestClient.

    The ``with`` block triggers the app lifespan (db_pool init), so the
    presets router can reach the test database.
    """
    from gateway.app import app

    with TestClient(app, base_url="http://test-gateway") as tc:
        monkeypatch.setattr("ava._gateway_transport._client", tc)
        yield


def _post_preset(name: str, label: str, **kw: object) -> None:
    """Create a preset through the wired TestClient."""
    import ava._gateway_transport as _client

    tc = cast(Any, _client._client)  # pyright: ignore[reportUnknownMemberType]
    assert tc is not None
    tc.post("/api/presets", json={"name": name, "label": label, **kw})


class TestList:
    def test_empty(self):
        """No presets → empty list."""
        from ava.agents.presets import list as list_presets

        result = cast(list[Any], list_presets())
        assert result == []

    def test_ordered_by_name(self):
        """Presets are returned in name order."""
        from ava.agents.presets import list as list_presets

        _post_preset("bbb", "B")
        _post_preset("aaa", "A")

        result = cast(list[Any], list_presets())
        assert [p.name for p in result] == ["aaa", "bbb"]

    def test_returns_preset_objects(self):
        """Each element is a Preset dataclass with all fields."""
        from ava.agents.presets import Preset
        from ava.agents.presets import list as list_presets

        _post_preset(
            "coder",
            "Coder",
            description="writes code",
            config={"llm_model": "m1"},
        )

        result = cast(list[Any], list_presets())
        assert len(result) == 1
        p = result[0]
        assert isinstance(p, Preset)
        assert p.name == "coder"
        assert p.label == "Coder"
        assert p.description == "writes code"
        assert p.config == {"llm_model": "m1"}


class TestGet:
    def test_found(self):
        """get(name) returns the matching preset."""
        from ava.agents.presets import Preset, get

        _post_preset("coder", "C")

        p = get("coder")
        assert isinstance(p, Preset)
        assert p.name == "coder"

    def test_not_found(self):
        """get(name) raises PresetNotFoundError when no preset matches."""
        from ava.agents.presets import PresetNotFoundError, get

        with pytest.raises(PresetNotFoundError) as exc:
            get("ghost")
        assert "ghost" in str(exc.value)
