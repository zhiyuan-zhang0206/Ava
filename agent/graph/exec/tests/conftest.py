"""Explicit formatter inputs independent of application configuration."""

import pytest

from agent.graph.exec.tests.output_inputs import CropConfig
from agent.graph.llm.tests.cancel_fixture import fake_cancel_event as fake_cancel_event
from base.clock import Clock, ClockConfig
from tests.fixtures.log_capture import loguru_records as loguru_records
from tests.fixtures.model_catalog import add_bindings as add_bindings
from tests.fixtures.model_catalog import add_models as add_models
from tests.fixtures.model_catalog import model_catalog as model_catalog
from tests.fixtures.model_catalog import set_prices as set_prices
from tests.fixtures.unit.config_authority import config_authority as config_authority
from tests.fixtures.unit.homes import unit_home as unit_home


@pytest.fixture
def crop_config() -> CropConfig:
    return CropConfig(
        exec_output_crop_after_lines=300,
        exec_output_crop_after_chars=65536,
        exec_output_crop_after_bytes=65536,
        exec_output_crop_head_lines=25,
        exec_output_crop_tail_lines=25,
        exec_output_crop_archive_max_bytes=16777216,
    )


@pytest.fixture
def output_clock() -> Clock:
    return Clock(ClockConfig("UTC", "UTC", False))
