"""GET /api/config/resolved — the per-model resolution view.

The endpoint is a read-only mirror of `shared/lm/registry.py:explain_setting`,
so the tests pin the three things a mirror can get wrong: it must enumerate
EXACTLY the per-model-defaultable set (no hand-maintained second list), it must
name the winning layer correctly for each of the three layers, and every row's
`name` must be a real config field so the panel can link back to that field's
editor.

A model with per-model tuning is injected into MODELS rather than asserted on a
real entry: the tuning column is empty today (#811 landed the mechanism, not the
values), and a test that waited for a real tuned model would silently stop
covering the model-default layer.
"""

from dataclasses import fields as dataclass_fields

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.config import field_names, settings
from shared.lm.registry import DEFAULT_TUNING, MODELS, ModelSpec, ModelTuning, tuning_field_names

TUNED_MODEL = "test-tuned-model"


@pytest.fixture
def tuned_model(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a throwaway model whose per-model layer has an opinion on one
    mechanical and one prompt-behavior field, leaving the rest to the floor."""
    monkeypatch.setitem(
        MODELS,
        TUNED_MODEL,
        ModelSpec(
            provider="claude",
            tuning=ModelTuning(auto_compact_fraction=0.55, agent_communication_style="silent"),
        ),
    )
    return TUNED_MODEL


def test_resolved_covers_every_tuning_field() -> None:
    """The row set IS ModelTuning — adding a tunable surfaces it with no second
    list to update, and every row keys a real config field so the frontend can
    link the row to that field's existing editor."""
    with TestClient(app) as client:
        resp = client.get("/api/config/resolved")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    names = [f["name"] for f in body["fields"]]
    assert names == list(tuning_field_names())
    assert len(names) == len(dataclass_fields(ModelTuning))
    assert set(names) <= field_names()
    # Every tunable is picked up by the agent process, so the panel's restart
    # hint is uniform — a field that ever says otherwise is a real drift signal.
    assert {f["restart_required"] for f in body["fields"]} == {"agent"}


def test_resolved_defaults_to_the_cluster_model() -> None:
    """An omitted `model` resolves against the cluster's own llm_model and echoes
    it back, so `curl /api/config/resolved` needs no argument to be useful."""
    with TestClient(app) as client:
        resp = client.get("/api/config/resolved")
    body = resp.json()
    assert body["model"] == settings.lm.llm_model
    assert body["registered"] is True


def test_shared_default_layer(tuned_model: str) -> None:
    """A field neither pinned nor tuned reports the DEFAULT_TUNING floor and
    says so — the shared floor is never presented as the model's own choice."""
    with TestClient(app) as client:
        resp = client.get("/api/config/resolved", params={"model": tuned_model})
    fields = {f["name"]: f for f in resp.json()["fields"]}

    row = fields["llm_retry_max_attempts"]
    assert row["source"] == "shared-default"
    assert row["effective_value"] == DEFAULT_TUNING.llm_retry_max_attempts
    assert row["shared_default"] == DEFAULT_TUNING.llm_retry_max_attempts
    assert row["model_default"] is None
    assert row["explicit_value"] is None


def test_model_default_layer(tuned_model: str) -> None:
    """A model's own tuning beats the floor, and the losing floor stays visible
    next to it (the point of the view: see the layer, not just the number)."""
    with TestClient(app) as client:
        resp = client.get("/api/config/resolved", params={"model": tuned_model})
    fields = {f["name"]: f for f in resp.json()["fields"]}

    row = fields["auto_compact_fraction"]
    assert row["source"] == "model-default"
    assert row["effective_value"] == 0.55
    assert row["model_default"] == 0.55
    assert row["shared_default"] == DEFAULT_TUNING.auto_compact_fraction
    assert row["explicit_value"] is None

    style = fields["agent_communication_style"]
    assert style["source"] == "model-default"
    assert style["effective_value"] == "silent"
    # Enum metadata rides along so a per-model row renders the same vocabulary
    # the field's own editor offers.
    assert style["field_type"] == "enum"
    assert "oriented" in (style["choices"] or [])


def test_explicit_layer_beats_the_model(tuned_model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `.env` value overrides the model's tuning for EVERY model —
    the documented cost of pinning one value cluster-wide, made visible."""
    monkeypatch.setattr(settings.agent, "auto_compact_fraction", 0.42)
    with TestClient(app) as client:
        resp = client.get("/api/config/resolved", params={"model": tuned_model})
    row = {f["name"]: f for f in resp.json()["fields"]}["auto_compact_fraction"]

    assert row["source"] == "explicit"
    assert row["effective_value"] == 0.42
    assert row["explicit_value"] == 0.42
    # The layers it shadowed stay reported, so the panel can show what pinning cost.
    assert row["model_default"] == 0.55
    assert row["shared_default"] == DEFAULT_TUNING.auto_compact_fraction


def test_unregistered_model_resolves_without_a_model_layer() -> None:
    """An id absent from the registry still resolves (exactly as at runtime — it
    simply has no per-model layer) and is flagged, so the panel can say that
    rather than present shared defaults as the model's own."""
    with TestClient(app) as client:
        resp = client.get("/api/config/resolved", params={"model": "no-such-model"})
    body = resp.json()
    assert body["registered"] is False
    assert body["model"] == "no-such-model"
    assert all(f["model_default"] is None for f in body["fields"])


def test_per_agent_flag_marks_the_overlay_reachable_fields() -> None:
    """`per_agent` is the backend's own flag, not a frontend guess — it marks the
    one layer above `explicit` that this cluster-wide view cannot show."""
    with TestClient(app) as client:
        resp = client.get("/api/config/resolved")
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["auto_compact_fraction"]["per_agent"] is True
    assert fields["reasoning_effort"]["per_agent"] is True
    # A cluster-pinned tunable is NOT overlay-reachable.
    assert fields["prompt_prefer_sdk_enabled"]["per_agent"] is False
