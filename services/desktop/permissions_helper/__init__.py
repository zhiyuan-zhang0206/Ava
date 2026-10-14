"""Permissions helper — the macOS desktop-automation daemon holding TCC grants.

`converge()` is the entry point for helper setup.
"""

from __future__ import annotations

from typing import Any

from base.host.permissions_helper.client import HelperFactory


def converge(*, helper: HelperFactory, timers: Any) -> None:
    """Idempotent macOS helper bring-up: build, sign, and load.

    `timers` is a `base.clock.Timers`, typed where it is used (`lifecycle.converge`): this door
    stays import-light because importing `base.clock` loads the configuration, which the test
    plugins that stub this function must not do.
    """
    from services.desktop.permissions_helper.lifecycle import converge as _converge

    _converge(helper=helper, timers=timers)
