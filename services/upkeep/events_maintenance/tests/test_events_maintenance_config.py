"""The events-maintenance composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from services.upkeep.events_maintenance import config as maintenance_config
from services.upkeep.events_maintenance import daemon


def test_the_slice_carries_the_live_value_of_every_field(
    process_config: ConfigBoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_config.view.daemon, "events_resolution_burst_threshold", 17)
    monkeypatch.setattr(process_config.view.general, "timezone", "Asia/Shanghai")
    config = daemon.events_maintenance_config(process_config)
    for field in dataclasses.fields(config):
        flat: Any = process_config.get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    assert config.events_resolution_burst_threshold == 17
    assert config.timezone == "Asia/Shanghai"


def test_two_entry_slices_remain_independent_at_each_lookup() -> None:
    first = ConfigBoot()
    first.read_process_environment()
    second = ConfigBoot()
    second.read_process_environment()
    first.set_field("events_resolution_burst_threshold", 17)
    second.set_field("events_resolution_burst_threshold", 23)
    assert daemon.events_maintenance_config(first).events_resolution_burst_threshold == 17
    assert daemon.events_maintenance_config(second).events_resolution_burst_threshold == 23
    first.set_field("events_resolution_burst_threshold", 31)
    assert daemon.events_maintenance_config(first).events_resolution_burst_threshold == 31
    assert daemon.events_maintenance_config(second).events_resolution_burst_threshold == 23


def test_database_slice_uses_the_supplied_owner_at_each_factory_call(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    first = ConfigBoot()
    first.read_process_environment()
    second = ConfigBoot()
    second.read_process_environment()
    first.set_field("db_pool_max_size", 13)
    second.set_field("db_pool_max_size", 19)
    captured: list[DbConfig] = []

    def database(
        inputs: DbConfig, *, gate: ProcessDbGate, local_host: Callable[[], str]
    ) -> Database:
        assert gate is database_gate
        captured.append(inputs)
        return Database(inputs, gate=gate, local_host=local_host)

    monkeypatch.setattr(maintenance_config, "Database", database)
    maintenance_config.events_maintenance_db(
        first, gate=database_gate, local_host=lambda: "127.0.0.1"
    )
    maintenance_config.events_maintenance_db(
        second, gate=database_gate, local_host=lambda: "127.0.0.1"
    )
    first.set_field("db_pool_max_size", 23)
    maintenance_config.events_maintenance_db(
        first, gate=database_gate, local_host=lambda: "127.0.0.1"
    )
    assert [inputs.db_pool_max_size for inputs in captured] == [13, 19, 23]
