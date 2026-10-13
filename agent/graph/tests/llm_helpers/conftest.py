"""Opt-in unit fixtures shared by this test directory."""

from agent.graph.tests.cancel_fixture import fake_cancel_event as fake_cancel_event
from base.log.tests.log_records import loguru_records as loguru_records
from base.paths.tests.homes import unit_home as unit_home
from tests.fixtures.model_catalog import add_bindings as add_bindings
from tests.fixtures.model_catalog import add_models as add_models
from tests.fixtures.model_catalog import model_catalog as model_catalog
from tests.fixtures.model_catalog import set_prices as set_prices
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.fixtures.unit.sdk import sdk_environment as sdk_environment
from tests.fixtures.unit.sdk import sdk_identity as sdk_identity
from tests.fixtures.unit.sdk import sdk_metering as sdk_metering
