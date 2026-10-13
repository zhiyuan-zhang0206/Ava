"""Layout of the event declarations: one domain module per file, nothing central to append to."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path
from types import ModuleType

import pytest

from base.events import declarations
from base.events.contract import EVENTS
from base.events.loader import merge_events
from base.events.vocabulary import EventSpec, telemetry_event

_EVENTS_DIR = Path(__file__).resolve().parents[1]
_BUILDERS = {"audit_event", "telemetry_event", "telemetry_audit_event", "EventSpec"}


def _domain_modules() -> list[tuple[str, ModuleType]]:
    return [
        (info.name, importlib.import_module(f"{declarations.__name__}.{info.name}"))
        for info in pkgutil.iter_modules(declarations.__path__)
    ]


def test_every_domain_module_declares_events_and_nothing_is_shadowed() -> None:
    modules = _domain_modules()
    assert modules
    total = 0
    for name, module in modules:
        events: dict[str, EventSpec] = module.EVENTS
        assert events, f"declarations/{name}.py declares no EVENTS"
        total += len(events)
    assert total == len(EVENTS)


def test_payloads_live_next_to_the_events_that_carry_them() -> None:
    for name, module in _domain_modules():
        events: dict[str, EventSpec] = module.EVENTS
        for event, spec in events.items():
            payload: type | None = spec.payload
            if payload is not None:
                assert payload.__module__ == module.__name__, (
                    f"{event}: payload {payload.__name__} belongs in declarations/{name}.py"
                )


def test_merge_rejects_a_name_declared_twice() -> None:
    spec = telemetry_event("dup_probe", "probe")
    with pytest.raises(ValueError, match="declared in both a and b"):
        merge_events([("a", {"dup_probe": spec}), ("b", {"dup_probe": spec})])


def test_merge_rejects_a_key_that_is_not_the_spec_name() -> None:
    with pytest.raises(ValueError, match="holds the spec for"):
        merge_events([("a", {"other": telemetry_event("dup_probe", "probe")})])


def test_events_are_not_declared_outside_the_domain_modules() -> None:
    """No central file to append to: the package's own modules hold the vocabulary,
    the derived views and the scan tool; an event goes in declarations/<domain>.py."""
    assert {p.name for p in _EVENTS_DIR.glob("*.py")} == {
        "__init__.py",
        "contract.py",
        "loader.py",
        "scan_kinds.py",
        "vocabulary.py",
    }
    tree = ast.parse((_EVENTS_DIR / "contract.py").read_text())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & _BUILDERS, "contract.py derives views; declare events in declarations/"
