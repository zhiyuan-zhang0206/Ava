"""Direct read-contract tests for the root-driver switch reader (task #3667).

`shared.root_driver.root_drive_enabled` is THE single reader of
`services.root_driver_enabled` — the cli driver and the frontend probe both
delegate to it. The consumer tests cover the switch end to end; these pin the
reader's own read rule: the value is read at call time, and an unreadable
configuration reads as OFF.
"""

from __future__ import annotations

import pytest

import shared.config as shared_config
from shared.config import settings
from shared.root_driver import root_drive_enabled


def test_reads_the_switch_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.services, "root_driver_enabled", False)
    assert root_drive_enabled() is False

    monkeypatch.setattr(settings.services, "root_driver_enabled", True)
    assert root_drive_enabled() is True

    monkeypatch.setattr(settings.services, "root_driver_enabled", False)
    assert root_drive_enabled() is False


def test_unreadable_configuration_reads_as_off(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken:
        @property
        def services(self) -> object:
            raise RuntimeError("configuration unreadable")

    monkeypatch.setattr(shared_config, "settings", _Broken())
    assert root_drive_enabled() is False
