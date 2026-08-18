"""The pytest process must carry a valid LC_ALL on macOS.

`tests/conftest.py` sets `LC_ALL=en_US.UTF-8` (setdefault, above every project
import) because Homebrew PostgreSQL's postmaster aborts with "postmaster became
multithreaded during startup" on macOS when the locale environment is missing:
locale init goes through CoreFoundation, which spawns a thread, and the
postmaster refuses to run multithreaded. The suite provisions its own throwaway
postmaster via `pg_ctl` (shared/pg_tools.py), which inherits this process's
environment, so a deleted or reordered conftest block breaks the whole suite on
macOS dev boxes — while CI stays green because Linux never hits the
CoreFoundation path.

These tests assert the *outcome* (the process env carries the locale) rather
than the mechanism, so a future change that reintroduces the missing locale by
some other route (a pytest plugin that clears env, a conftest reorder that
lands after a postmaster spawn) still fails loudly. A dev shell that already
exports its own valid LC_ALL is fine — conftest's setdefault leaves it alone
and the postmaster is happy; only the missing case is the regression.
"""

from __future__ import annotations

import os

import pytest

import shared.platform as plat

pytestmark = pytest.mark.skipif(not plat.IS_MACOS, reason="macOS-only postmaster locale check")


def test_pytest_env_carries_lc_all_on_macos() -> None:
    """The suite's own postmaster (pg_ctl, inheriting this env) needs LC_ALL."""
    assert os.environ.get("LC_ALL"), (
        "tests/conftest.py must set LC_ALL on macOS — Homebrew PG's postmaster "
        "aborts with 'postmaster became multithreaded during startup' otherwise"
    )
