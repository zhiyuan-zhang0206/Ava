"""Telemetry tests leave the core metric registry as they found it.

The core definition modules register once, at first import. Tests here empty the
registry or rebuild part of it, and nothing imports the modules again, so a test
that runs later in the same process would otherwise see a registry missing the
shipped metrics.
"""

from collections.abc import Iterator

import pytest

from base.telemetry.metrics.core import catalog


@pytest.fixture(autouse=True)
def _core_registry_restored() -> Iterator[None]:
    shipped = catalog.collect_core_metrics()
    yield
    catalog.clear_core_registry()
    for spec in shipped:
        catalog.register_core_metric(spec)
