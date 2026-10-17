import pytest

from agent.graph.exec.tests.cancel_fixture import fake_cancel_event as fake_cancel_event
from agent.graph.exec.tests.output_inputs import CropConfig
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.config_authority import config_authority as config_authority
from base.log.tests.log_records import loguru_records as loguru_records
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from meiva.tests.fixtures.sdk import sdk_environment as sdk_environment
from meiva.tests.fixtures.sdk import sdk_identity as sdk_identity
from meiva.tests.fixtures.sdk import sdk_metering as sdk_metering


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
    return Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS)
