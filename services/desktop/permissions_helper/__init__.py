"""Permissions helper — the macOS desktop-automation daemon holding TCC grants.

`converge()` is the entry point for helper setup.
"""

from __future__ import annotations


def converge() -> None:
    """Idempotent macOS helper bring-up: build, sign, and load."""
    from services.desktop.permissions_helper.lifecycle import converge as _converge

    _converge()
