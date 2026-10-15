from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest


def plugin_registrations_present() -> bool:
    installation = sys.modules.get("ava.sdk_surface.installation")
    return installation is not None and installation.installed() is not None


def drop_plugin_registrations() -> None:
    install = sys.modules.get("ava.sdk_surface.install")
    if install is not None:
        install.uninstall()


@pytest.fixture(autouse=True)
def _restore_plugin_registrations() -> Iterator[None]:
    started_empty = not plugin_registrations_present()
    yield
    if started_empty and plugin_registrations_present():
        drop_plugin_registrations()
