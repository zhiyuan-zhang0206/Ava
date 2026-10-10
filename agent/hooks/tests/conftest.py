"""Opt-in unit fixtures shared by this test directory."""

from agent.graph.llm.tests.cancel_fixture import fake_cancel_event as fake_cancel_event
from tests.fixtures.log_capture import loguru_records as loguru_records
from tests.fixtures.model_catalog import add_bindings as add_bindings
from tests.fixtures.model_catalog import add_models as add_models
from tests.fixtures.model_catalog import model_catalog as model_catalog
from tests.fixtures.model_catalog import set_prices as set_prices
from tests.fixtures.unit.homes import unit_home as unit_home
