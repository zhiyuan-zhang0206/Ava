from __future__ import annotations

from typing import Any

import pytest

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.packages.plugins import activation


def _spy_emit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str]]:
    emitted: list[tuple[str, str, str, str]] = []

    def spy(plugin: str, surface: str, identifier: str, detail: str, _model: str) -> None:
        emitted.append((plugin, surface, identifier, detail))

    monkeypatch.setattr(activation, "emit", spy)
    return emitted


def test_records_an_attributed_firing(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted = _spy_emit(monkeypatch)

    activation.record(
        "meiva_syntax_fix", "hooks", "before_exec", detail="wrote messages", model="m"
    )

    assert emitted == [("meiva_syntax_fix", "hooks", "before_exec", "wrote messages")]


def test_unattributed_firing_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted = _spy_emit(monkeypatch)

    activation.record(None, "hooks", "before_llm", detail="wrote messages", model="m")

    assert emitted == []


def test_event_carries_the_model_in_force(loguru_records: LogRecords) -> None:
    activation.emit("meiva_code", "sdkWraps", "files.read", "inner_calls=0", "deepseek-flash")

    logged = loguru_records.events(activation.PLUGIN_ACTIVATION_EVENT)
    assert len(logged) == 1
    payload = logged[0]["extra"]
    assert payload["event"] == activation.PLUGIN_ACTIVATION_EVENT
    assert payload["plugin"] == "meiva_code"
    assert payload["surface"] == "sdkWraps"
    assert payload["identifier"] == "files.read"
    assert payload["detail"] == "inner_calls=0"
    assert payload["model"] == "deepseek-flash"


def test_emit_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:

    class _Broken:
        def bind(self, **_kwargs: Any) -> None:
            raise RuntimeError("sink down")

    monkeypatch.setattr(activation, "logger", _Broken())

    activation.record("meiva_memory", "hooks", "after_exec", detail="wrote messages", model="m")


def test_registered_event_name_is_on_contract() -> None:
    from base.events.contract import EVENTS, payload_keys

    spec = EVENTS[activation.PLUGIN_ACTIVATION_EVENT]
    assert spec.category == "telemetry"
    assert set(payload_keys(activation.PLUGIN_ACTIVATION_EVENT)) == {
        "plugin",
        "surface",
        "identifier",
        "detail",
        "model",
    }
