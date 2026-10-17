"""The explicit SDK workspace environment, separate from bare home fixtures."""

from pathlib import Path

import pytest

from meiva.tests.fixtures.pin_agent import pin_agent


@pytest.fixture
def workspace(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This test's agent workspace dir (the relative-path base of meiva.files /
    meiva.shell.run / meiva.understand path mode), under the per-test unit home.

    Pins the agent id explicitly instead of relying on the session default context
    (the consumer-local SDK environment) staying unmutated across test
    ordering (a leak through it is exactly what the `_isolated_agent`
    fix in meiva/tests/fixtures/meiva_tests.py guards against). The dir is NOT
    pre-created — `workspace_dir` mkdirs on first resolution, and several
    tests assert exactly that; pre-create with `.mkdir(parents=True)` when a
    test seeds files into it.
    """
    pin_agent(1)
    return unit_home / "workspaces" / "1"
