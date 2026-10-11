"""Private native-command protection belongs to the helper's own tests."""

import pytest

from tests.fixtures.unit.homes import default_home as default_home
from tests.fixtures.unit.homes import unit_home as unit_home


@pytest.fixture(autouse=True)
def _helper_native_commands(permissions_helper_native_io: None) -> None:
    """Caller tests mock the real command seam after this fail-closed default."""
