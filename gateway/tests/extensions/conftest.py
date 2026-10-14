"""Consumer-local opt-in fixtures for this test directory."""

from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from tests.fixtures.unit.config_authority import config_authority as config_authority
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.fixtures.unit.identity import set_machine_identity as set_machine_identity
from tests.fixtures.unit.sdk import sdk_environment as sdk_environment
from tests.fixtures.unit.sdk import sdk_identity as sdk_identity
from tests.fixtures.unit.sdk import sdk_metering as sdk_metering
