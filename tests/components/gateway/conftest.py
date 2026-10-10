"""Opt-in unit fixtures shared by this test directory."""

from base.deploy.lifecycle.tests.serving_root import serving_root as serving_root
from tests.fixtures.model_catalog import add_bindings as add_bindings
from tests.fixtures.model_catalog import add_models as add_models
from tests.fixtures.model_catalog import model_catalog as model_catalog
from tests.fixtures.model_catalog import set_prices as set_prices
from tests.fixtures.unit.config_authority import config_authority as config_authority
from tests.fixtures.unit.homes import unit_home as unit_home
from tests.fixtures.unit.identity import set_machine_identity as set_machine_identity
