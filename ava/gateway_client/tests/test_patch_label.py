"""`ava.gateway_client.patch_label` — the wire shape of an agent's label change."""

import httpx
import pytest


def test_patch_label_sends_the_label_and_who_set_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import ava.gateway_client as gc

    sent: list[tuple[str, dict[str, object] | None]] = []

    def fake_patch(path: str, json: dict[str, object] | None = None) -> httpx.Response:
        sent.append((path, json))
        return httpx.Response(204)

    monkeypatch.setattr(gc, "patch", fake_patch)
    gc.patch_label(42, "lead")
    gc.patch_label(42, "", source="user")

    assert sent == [
        ("/api/agents/42", {"label": "lead", "source": "self"}),
        ("/api/agents/42", {"label": "", "source": "user"}),
    ]
