"""Opt-in unit fixtures shared by the namespace tests: one binding for every directory below.

The four directory conftests held the same fixtures (a union of what any of them requested);
they are requested by name, so one wider scope changes nothing for a test that does not ask.
"""

from base.host.net.tests.retry_waits import retry_waits as retry_waits
from base.log.tests.log_records import loguru_records as loguru_records
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from tests.fixtures.unit.config_authority import config_authority as config_authority
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.fixtures.unit.identity import set_machine_identity as set_machine_identity
from tests.fixtures.unit.model_owner import model_installation as model_installation
from tests.fixtures.unit.model_owner import sdk_model_owner as sdk_model_owner
from tests.fixtures.unit.sdk import sdk_environment as sdk_environment
from tests.fixtures.unit.sdk import sdk_identity as sdk_identity
from tests.fixtures.unit.sdk import sdk_metering as sdk_metering
