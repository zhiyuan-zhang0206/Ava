from __future__ import annotations

from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
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
