"""Unit tests for shared.resource_sample — the one-shot degraded reading.

The point of the module after issue #46 is that it keeps NOTHING: Prometheus
holds the host time series, so a second retained history here would be a second
answer that drifts. These tests pin that (stateless, self-consistent numbers)
rather than re-testing psutil.
"""

from __future__ import annotations

import psutil
import pytest

from shared.resource_sample import _CPU_INTERVAL_S, ResourceSample, resource_sample


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
