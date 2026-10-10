"""SDK operation clocks retain their real installation or context owner."""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import patch

import pytest

import ava
from ava.sdk_surface import install, settings
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.clock import Clock, clock_config_from_boot
from base.config import ConfigBoot
from base.packages.plugins.extensions import ExtensionRegistry


@pytest.fixture
def clock_scope() -> Iterator[None]:
    prior_context = ava.unbind_context()
    prior = install.uninstall()
    try:
        with patch.dict(os.environ):
            yield
    finally:
        current = install.uninstall()
        if current is not None:
            current.sampling.close()
        if prior is not None:
            install.install(
                prior.registry,
                catalog=prior.catalog,
                authority=prior.authority,
                delivery_sender=prior.delivery_sender,
                sampling=prior.sampling,
                clock_factory=prior.clock_factory,
            )
        if prior_context is not None:
            ava.bind_context(prior_context)


def test_bare_sdk_clock_uses_live_installation_owner(clock_scope: None) -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("timezone", "UTC")
    second.set_field("timezone", "Asia/Shanghai")
    calls: list[Clock] = []

    def clock() -> Clock:
        value = Clock(clock_config_from_boot(first))
        calls.append(value)
        return value

    install.install(ExtensionRegistry(()), clock_factory=clock)
    assert not calls
    before = settings.clock()
    assert before.authoritative_timezone == "UTC"
    first.set_field("timezone", "Asia/Tokyo")
    after = settings.clock()
    assert after.authoritative_timezone == "Asia/Tokyo"
    assert before.authoritative_timezone == "UTC"
    assert Clock(clock_config_from_boot(second)).authoritative_timezone == "Asia/Shanghai"
    assert len(calls) == 2


def test_bound_sdk_clock_uses_context_without_borrowing_installation(clock_scope: None) -> None:
    boot = ConfigBoot()
    boot.set_field("timezone", "UTC")

    def unrelated() -> Clock:
        raise AssertionError("a bound operation must keep its context's clock owner")

    install.install(ExtensionRegistry(()), clock_factory=unrelated)
    ava.bind_context(
        AvaContext(clients=ClientSet(), clock_factory=lambda: Clock(clock_config_from_boot(boot)))
    )
    try:
        assert settings.clock().authoritative_timezone == "UTC"
    finally:
        ava.unbind_context()


def test_partial_context_refuses_clock_instead_of_falling_back(clock_scope: None) -> None:
    boot = ConfigBoot()
    install.install(
        ExtensionRegistry(()), clock_factory=lambda: Clock(clock_config_from_boot(boot))
    )
    ava.bind_context(AvaContext(clients=ClientSet()))
    try:
        with pytest.raises(RuntimeError, match="clock"):
            settings.clock()
    finally:
        ava.unbind_context()


def test_inventory_installation_remains_cold_and_refuses_clock(clock_scope: None) -> None:
    install.install(ExtensionRegistry(()))
    with pytest.raises(RuntimeError, match="Clock factory"):
        settings.clock()
