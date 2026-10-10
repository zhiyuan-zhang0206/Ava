"""Merge the per-domain event declarations of ``base/events/declarations/``.

``load_events`` imports every module of that package and merges their ``EVENTS``, so a new
domain is a new file there with nothing else to register.
"""

import pkgutil
from collections.abc import Iterable, Mapping

from base.events import declarations
from base.events.vocabulary import EventSpec
from base.packages.declared_inputs import declared_import


def merge_events(modules: Iterable[tuple[str, Mapping[str, EventSpec]]]) -> dict[str, EventSpec]:
    """Merge ``(module name, events)`` pairs in order; a repeated or mis-keyed name is an error."""
    merged: dict[str, EventSpec] = {}
    owner: dict[str, str] = {}
    for module, events in modules:
        for name, spec in events.items():
            if spec.name != name:
                raise ValueError(f"{module}: key {name!r} holds the spec for {spec.name!r}")
            if name in merged:
                raise ValueError(f"event {name!r} is declared in both {owner[name]} and {module}")
            merged[name] = spec
            owner[name] = module
    return merged


def load_events() -> dict[str, EventSpec]:
    """Every declared event, merged in module-name order."""
    names = sorted(info.name for info in pkgutil.iter_modules(declarations.__path__))
    return merge_events(
        (
            name,
            declared_import(
                f"{declarations.__name__}.{name}", within=("base.events.declarations.*",)
            ).EVENTS,
        )
        for name in names
    )
