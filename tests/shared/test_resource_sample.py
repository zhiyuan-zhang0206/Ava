"""Unit tests for shared.resource_sample — the one-shot degraded reading.

The point of the module after issue #46 is that it keeps NOTHING: Prometheus
holds the host time series, so a second retained history here would be a second
answer that drifts. These tests pin that (stateless, self-consistent numbers)
rather than re-testing psutil.
"""

from __future__ import annotations

import psutil
import pytest

from shared.resource_sample import (
    _CPU_INTERVAL_S,
    ResourceSample,
    _battery_sample,
    _parse_battery,
    resource_sample,
)


class TestResourceSample:
    def test_returns_a_self_consistent_reading(self) -> None:
        s = resource_sample()
        assert isinstance(s, ResourceSample)
        assert s.cpu_pct >= 0.0
        assert 0.0 <= s.mem_pct <= 100.0
        assert 0.0 <= s.disk_pct <= 100.0
        # used/total must agree with the shown percent — the panel renders all
        # three and a mismatched denominator is what the derivation fixes.
        assert s.mem_used_gb <= s.mem_total_gb
        assert s.disk_used_gb <= s.disk_total_gb
        assert s.mem_total_gb > 0

    def test_cpu_is_measured_not_inherited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CPU comes from a BLOCKING psutil measurement.

        With `interval=None` psutil reports the average since the previous call
        in this process — the ring buffer's primed baseline is what made that
        meaningful, and it is gone. A stateless caller passing None would read
        0.0 on its first call and an arbitrary window afterwards, so the
        interval being a real duration is the invariant, not the value.
        """
        intervals: list[float | None] = []
        real = psutil.cpu_percent

        def spy(interval: float | None = None) -> float:
            intervals.append(interval)
            return real(interval=interval)

        monkeypatch.setattr(psutil, "cpu_percent", spy)
        first = resource_sample()
        second = resource_sample()
        assert intervals == [_CPU_INTERVAL_S, _CPU_INTERVAL_S]
        assert second.ts >= first.ts

    def test_is_frozen(self) -> None:
        s = resource_sample()
        with pytest.raises(Exception, match="frozen"):
            s.cpu_pct = 1.0

    def test_propagates_psutil_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The module does not swallow a read failure — the status callers own
        the degrade decision (a machine row without a reading), not this."""
        monkeypatch.setattr(
            psutil, "virtual_memory", lambda: (_ for _ in ()).throw(RuntimeError("no psutil"))
        )
        with pytest.raises(RuntimeError, match="no psutil"):
            resource_sample()


class TestBatterySample:
    """Battery fields ride the same one-shot sample (task #3743).

    macOS only, best-effort: read failures degrade to None fields instead of
    failing the probe, and a machine with no battery reports nothing.
    """

    PMSET_CHARGING = (
        "Now drawing from 'AC Power'\n"
        " -InternalBattery-0 (id=26607715)\t80%; charging; 1:03 remaining present: true\n"
    )
    PMSET_DISCHARGING = (
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=1)\t23%; discharging; 3:47 remaining present: true\n"
    )
    PMSET_NO_ESTIMATE = (
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=1)\t42%; discharging; (no estimate) present: true\n"
    )
    PMSET_CHARGED = (
        "Now drawing from 'AC Power'\n"
        " -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n"
    )
    PMSET_DESKTOP = "Now drawing from 'AC Power'\n"

    def test_parse_charging_on_ac(self) -> None:
        assert _parse_battery(self.PMSET_CHARGING) == {
            "battery_percent": 80,
            "battery_power": "ac",
            "battery_charging": True,
            "battery_remaining_min": 63,
        }

    def test_parse_discharging(self) -> None:
        assert _parse_battery(self.PMSET_DISCHARGING) == {
            "battery_percent": 23,
            "battery_power": "battery",
            "battery_charging": False,
            "battery_remaining_min": 227,
        }

    def test_no_estimate_omits_remaining_only(self) -> None:
        assert _parse_battery(self.PMSET_NO_ESTIMATE) == {
            "battery_percent": 42,
            "battery_power": "battery",
            "battery_charging": False,
        }

    def test_charged_is_not_charging(self) -> None:
        assert _parse_battery(self.PMSET_CHARGED) == {
            "battery_percent": 100,
            "battery_power": "ac",
            "battery_charging": False,
            "battery_remaining_min": 0,
        }

    def test_desktop_reports_nothing(self) -> None:
        assert _parse_battery(self.PMSET_DESKTOP) == {}

    def test_off_macos_reports_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import shared.resource_sample as module

        monkeypatch.setattr(module.sys, "platform", "linux")
        assert _battery_sample() == {}

    def test_read_failure_degrades_to_no_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        import shared.resource_sample as module

        def _raise_timeout(*_a: object, **_k: object) -> object:
            raise subprocess.TimeoutExpired("pmset", 2)

        monkeypatch.setattr(module.subprocess, "run", _raise_timeout)
        assert _battery_sample() == {}

    def test_sample_carries_battery_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import shared.resource_sample as module

        monkeypatch.setattr(module, "_battery_sample", lambda: _parse_battery(self.PMSET_CHARGING))
        s = resource_sample()
        assert s.battery_percent == 80
        assert s.battery_power == "ac"
        assert s.battery_charging is True
        assert s.battery_remaining_min == 63
