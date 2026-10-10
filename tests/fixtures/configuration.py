"""Explicit, one-time configuration snapshots for in-process test roots."""

import os
from unittest.mock import patch

from base.config import ConfigBoot, field_domain, field_names, get_field, settings


def snapshot_process_config() -> ConfigBoot:
    """Preserve current test values and explicit origins in an independent owner.

    Delivery belongs to this bootstrap call. Later operation-time changes must
    target the returned owner; there is no synchronization with legacy settings.
    """
    values = {name: get_field(name) for name in field_names()}
    explicit = {
        domain: set(getattr(settings, domain).model_fields_set)
        for domain in {field_domain(name) for name in values}
    }
    owner = ConfigBoot()
    with patch.dict(os.environ):
        model = owner.ensure_eager()
    for name, value in values.items():
        owner.set_field(name, value)
    for domain, fields in explicit.items():
        destination = getattr(model, domain).model_fields_set
        destination.clear()
        destination.update(fields)
    return owner
