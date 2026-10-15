import os
from collections.abc import Callable
from dataclasses import fields
from typing import Any
from unittest.mock import patch

from base.config import ConfigBoot, field_domain, field_names, get_field, settings
from base.host.env.agent_slices import ModelOverrides


def snapshot_process_config() -> ConfigBoot:
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
    explicit: dict[tuple[str, str], Any] = {}
    for key, value in changes.items():
        domain, _, field = key.partition("__")
        getattr(getattr(settings, domain), field)
        explicit[(domain, field)] = value

    def read(domain: str, field: str) -> Any:
        if (domain, field) in explicit:
            return explicit[(domain, field)]
        return getattr(getattr(settings, domain), field)

    return read


def model_overrides(**changes: Any) -> ModelOverrides:
    pins = {field.name: get_field(field.name) for field in fields(ModelOverrides)}
    unknown = sorted(set(changes) - set(pins))
    if unknown:
        raise TypeError(f"not ModelOverrides fields: {unknown}")
    return ModelOverrides.from_pins({**pins, **changes})
