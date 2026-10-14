"""Consumer-local configuration authority for an explicit private unit home."""

from pathlib import Path

import pytest

from base.config import Settings, settings
from base.config.service_read import ConfigAuthority


@pytest.fixture
def config_authority(unit_home: Path) -> ConfigAuthority:
    """The test's explicit boot models and unit file, with no installed holder."""
    complete = settings if settings.profile is None else Settings(profile=None)
    return ConfigAuthority(settings, complete, unit_home / ".env")
