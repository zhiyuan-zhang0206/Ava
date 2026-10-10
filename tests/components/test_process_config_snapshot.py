"""Test process roots preserve configuration values and their explicit origins."""

import pytest

from base.clock import Clock, clock_config_from_boot
from base.config import field_names, get_field, settings
from gateway import app as gateway_app
from tests.fixtures.configuration import snapshot_process_config
from tests.path_scoped.gateway_tests import gateway_config_owner as gateway_config_owner


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
def test_session_snapshot_preserves_timezone_origin(explicit: bool) -> None:
    original_value = settings.general.timezone
    original_fields = set(settings.general.model_fields_set)
    timezone = "Asia/Tokyo" if explicit else "America/Los_Angeles"
    try:
        settings.general.timezone = timezone
        if not explicit:
            settings.general.model_fields_set.discard("timezone")
        owner = snapshot_process_config()
        assert owner.field_explicitly_set("timezone") is explicit
        clock = Clock(clock_config_from_boot(owner))
        assert clock.timezone == timezone
        assert clock.authoritative_timezone == (timezone if explicit else None)
        assert settings.general.model_fields_set == (
            original_fields | {"timezone"} if explicit else original_fields - {"timezone"}
        )
    finally:
        settings.general.timezone = original_value
        settings.general.model_fields_set.clear()
        settings.general.model_fields_set.update(original_fields)


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
def test_gateway_snapshot_preserves_all_values_and_timezone_origin(explicit: bool) -> None:
    original_value = settings.general.timezone
    original_fields = set(settings.general.model_fields_set)
    timezone = "Asia/Tokyo" if explicit else "America/Los_Angeles"
    try:
        settings.general.timezone = timezone
        if not explicit:
            settings.general.model_fields_set.discard("timezone")
        values = {name: get_field(name) for name in field_names()}
        owner = gateway_app.ConfigBoot()
        assert {name: owner.get_field(name) for name in values} == values
        assert owner.field_explicitly_set("timezone") is explicit
        clock = Clock(clock_config_from_boot(owner))
        assert clock.timezone == timezone
        assert clock.authoritative_timezone == (timezone if explicit else None)
        assert settings.general.model_fields_set == (
            original_fields | {"timezone"} if explicit else original_fields - {"timezone"}
        )
    finally:
        settings.general.timezone = original_value
        settings.general.model_fields_set.clear()
        settings.general.model_fields_set.update(original_fields)
