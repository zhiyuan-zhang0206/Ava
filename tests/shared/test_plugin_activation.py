"""Unit tests for `shared/plugin_activation.py` — the injection-surface
activation recorder (issue #40, philosophy §6).

Pins the three properties that make it safe to hang off every plugin surface:
only plugin registrations are recorded, the event carries the model so the
obsolescence gauge is answerable per model, and recording is a pure side
channel that swallows its own failures.
"""

from __future__ import annotations

from typing import Any

import pytest

from shared import plugin_activation


def _spy_emit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str]]:
    """Capture (plugin, surface, identifier, detail) per emitted event."""
    emitted: list[tuple[str, str, str, str]] = []

    def spy(plugin: str, surface: str, identifier: str, detail: str) -> None:
        emitted.append((plugin, surface, identifier, detail))

    monkeypatch.setattr(plugin_activation, "emit", spy)
    return emitted


def test_records_an_attributed_firing(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted = _spy_emit(monkeypatch)

    plugin_activation.record("ava_syntax_fix", "hooks", "before_exec", detail="wrote messages")

    assert emitted == [("ava_syntax_fix", "hooks", "before_exec", "wrote messages")]


def test_unattributed_firing_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`plugin=None` is a framework registration or a test's direct call — the
    same gate `plugin_contributions.record` applies to the ledger, so the two
    stay parallel and the framework never shows up as a plugin."""
    emitted = _spy_emit(monkeypatch)

    plugin_activation.record(None, "hooks", "before_llm", detail="wrote messages")

    assert emitted == []


def test_event_carries_the_model_in_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """Philosophy §6 asks for activation telemetry *per model*: without the
    model on the event, "does model X still need this shim" is unanswerable."""
    bound: list[dict[str, Any]] = []

    class _Logger:
        def bind(self, **kwargs: Any) -> _Logger:
            bound.append(kwargs)
            return self

        def info(self, _msg: str) -> None: ...

    monkeypatch.setattr(plugin_activation, "logger", _Logger())

    plugin_activation.emit("ava_code", "sdkWraps", "files.read", "inner_calls=0")

    assert len(bound) == 1
    payload = bound[0]
    assert payload["event"] == plugin_activation.PLUGIN_ACTIVATION_EVENT
    assert payload["plugin"] == "ava_code"
    assert payload["surface"] == "sdkWraps"
    assert payload["identifier"] == "files.read"
    assert payload["detail"] == "inner_calls=0"
    assert payload["model"]  # whatever the cluster is configured with, never blank


def test_emit_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Side-channel contract: a broken sink must not raise into the hook / wrap
    it was called from."""

    class _Broken:
        def bind(self, **_kwargs: Any) -> None:
            raise RuntimeError("sink down")

    monkeypatch.setattr(plugin_activation, "logger", _Broken())

    plugin_activation.record("ava_memory", "hooks", "after_exec", detail="wrote messages")


def test_registered_event_name_is_on_contract() -> None:
    """The emitter fails loud on an unregistered event name, so the registry
    entry is part of the contract, not an optional extra."""
    from shared.events.contract import EVENTS, payload_keys

    spec = EVENTS[plugin_activation.PLUGIN_ACTIVATION_EVENT]
    assert spec.category == "telemetry"
    assert set(payload_keys(plugin_activation.PLUGIN_ACTIVATION_EVENT)) == {
        "plugin",
        "surface",
        "identifier",
        "detail",
        "model",
    }
