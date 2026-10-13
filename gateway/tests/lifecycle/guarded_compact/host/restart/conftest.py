"""Consumer-local opt-in fixtures for this test directory."""

import os
from pathlib import Path

import pytest

from base.config import Settings, settings
from base.config.service_read import ConfigAuthority
from tests.fixtures.model_catalog import add_bindings as add_bindings
from tests.fixtures.model_catalog import add_models as add_models
from tests.fixtures.model_catalog import model_catalog as model_catalog
from tests.fixtures.model_catalog import set_prices as set_prices


@pytest.fixture
def config_authority() -> ConfigAuthority:
    """The session home's authority, not a bare unit home.

    These tests start child processes that inherit `AVA_HOME`. A child reads the
    cluster values the suite declares in the session home's `.env`; a bare unit
    home declares none, so the child would dial the unprovisioned sentinel. The
    gateway scope asks for an authority only to serve config ops these tests never
    issue, so it is built over the session home they keep.
    """
    complete = settings if settings.profile is None else Settings(profile=None)
    return ConfigAuthority(settings, complete, Path(os.environ["AVA_HOME"]) / ".env")
