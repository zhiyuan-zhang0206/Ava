"""Permissions helper — the macOS desktop-automation daemon holding TCC grants.

`converge()` is the entry point for helper setup.
"""

from __future__ import annotations

from base.host.permissions_helper.client import HelperFactory


def converge(*, helper: HelperFactory) -> None:
    """Idempotent macOS helper bring-up: build, sign, and load."""
    from services.desktop.permissions_helper.lifecycle import converge as _converge

    _converge(helper=helper)
