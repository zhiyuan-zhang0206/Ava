"""Keep every directory in one collector node, so its conftest reaches all its tests.

pytest 9 binds a conftest's fixtures to the first `Directory` node collected
for its directory and matches fixtures by node identity. Positional paths that
leave a directory and come back to it (`tests/components/agent/a.py tests/b.py
tests/components/agent/c.py`) make pytest build a second `Directory` node for the same
directory, and every test under it silently loses that conftest's fixtures —
autouse isolation fixtures included. So the positional paths are grouped by
directory before collection, and a collection that still splits a directory
stops the run instead of running tests without their fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    # Sorting by path parts makes every shared directory prefix contiguous.
    config.args[:] = sorted(
        config.args, key=lambda arg: Path(arg.split("::", 1)[0]).absolute().parts
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    first: dict[str, pytest.Directory] = {}
    for item in items:
        for parent in item.iter_parents():
            if not isinstance(parent, pytest.Directory):
                continue
            if first.setdefault(parent.nodeid, parent) is not parent:
                raise pytest.UsageError(
                    f"directory {parent.nodeid or '.'!r} was collected twice, so pytest "
                    "hides its conftest.py fixtures (autouse ones included) from the "
                    "second visit. Group the paths by directory and run again."
                )
