"""Opt-in unit fixtures shared by this test directory."""

from ava.sdk_surface.tests.agent_binding import bound_agent as bound_agent
from ava.sdk_surface.tests.agent_binding import no_agent as no_agent
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.config_authority import config_authority as config_authority
from base.host.net.tests.retry_waits import retry_waits as retry_waits
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
