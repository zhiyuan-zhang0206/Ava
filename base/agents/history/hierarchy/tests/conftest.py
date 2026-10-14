"""Opt-in unit fixtures shared by this test directory."""

from base.log.tests.log_records import loguru_records as loguru_records
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from tests.fixtures.unit.config_authority import config_authority as config_authority
