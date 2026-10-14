"""Explicit, one-time configuration snapshots for in-process test roots."""

import os
from collections.abc import Callable
from dataclasses import fields
from typing import Any
from unittest.mock import patch

from base.config import ConfigBoot, field_domain, field_names, get_field, settings
from base.host.env.agent_slices import ModelOverrides


def snapshot_process_config() -> ConfigBoot:
    """Preserve current test values and explicit origins in an independent owner.

    Delivery belongs to this bootstrap call. Later operation-time changes must
    target the returned owner; there is no synchronization with legacy settings.
    """
    authority = settings.env_boot
    values = {name: get_field(name) for name in field_names()}
    explicit = {
        domain: set(getattr(settings, domain).model_fields_set)
        for domain in {field_domain(name) for name in values}
    }
    owner = ConfigBoot()
    with patch.dict(os.environ):
        model = owner.ensure_eager()
    owner.env_boot = authority
    model.env_boot = authority
    for name, value in values.items():
        owner.set_field(name, value)
    for domain, names in explicit.items():
        destination = getattr(model, domain).model_fields_set
        destination.clear()
        destination.update(names)
    return owner


def defaults(**changes: Any) -> Callable[[str, str], Any]:
    """A `default_reader` for `AgentSlices.resolve`: the unit environment's value of `domain.field`,
    except the fields named in `changes` as `domain__field=value`.

    Replaces patching `settings.<domain>.<field>` and reading it back: the test hands the
    resolution the exact values it wants. The unit environment is read lazily, so building the
    reader constructs nothing. A name that is not a configuration field raises at once.
    """
    explicit: dict[tuple[str, str], Any] = {}
    for key, value in changes.items():
        domain, _, field = key.partition("__")
        getattr(getattr(settings, domain), field)  # fail fast on an unknown domain or field
        explicit[(domain, field)] = value

    def read(domain: str, field: str) -> Any:
        if (domain, field) in explicit:
            return explicit[(domain, field)]
        return getattr(getattr(settings, domain), field)

    return read


def model_overrides(**changes: Any) -> ModelOverrides:
    """The unit environment's `ModelOverrides`, except the fields named in `changes`.

    Replaces patching `settings.<domain>.<field>` and building the overrides from the global:
    the test states the exact explicit values it hands the resolution. A name that is not a
    `ModelOverrides` field raises at once.
    """
    pins = {field.name: get_field(field.name) for field in fields(ModelOverrides)}
    unknown = sorted(set(changes) - set(pins))
    if unknown:
        raise TypeError(f"not ModelOverrides fields: {unknown}")
    return ModelOverrides.from_pins({**pins, **changes})
