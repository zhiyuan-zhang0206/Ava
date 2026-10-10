"""The SDK test root shares the prepared configuration rather than booting a bare home."""

from pathlib import Path

import pytest

import ava
from base.agents.context import AvaContext
from base.config import ConfigBoot
from tests.fixtures.pin_agent import pin_agent


def test_sdk_context_uses_the_prepared_owner_with_a_bare_home(
    unit_home: Path, process_config: ConfigBoot, _sdk_environment: AvaContext
) -> None:
    assert list(unit_home.iterdir()) == []
    assert ava.context is _sdk_environment
    assert ava.context.clients is _sdk_environment.clients
    assert ava.self.AGENT_ID == 1
    assert process_config.view.data_plane.db_url != ""
    assert ava.context.clock_factory is not None
    clock = ava.context.clock_factory()
    assert clock.timezone == process_config.view.general.timezone
    assert clock.authoritative_timezone == (
        process_config.view.general.timezone
        if process_config.field_explicitly_set("timezone")
        else None
    )


def test_repinning_borrows_the_same_clients_and_clock(_sdk_environment: AvaContext) -> None:
    pin_agent(91)
    assert ava.self.AGENT_ID == 91
    assert ava.context.clients is _sdk_environment.clients
    assert ava.context.clock_factory is _sdk_environment.clock_factory


def test_the_following_case_receives_the_restored_session_context(
    _sdk_environment: AvaContext, request: pytest.FixtureRequest
) -> None:
    assert ava.context is _sdk_environment
    assert ava.self.AGENT_ID == 1
    assert {"_restore_agent_identity", "_restore_metering"} <= set(request.fixturenames)
