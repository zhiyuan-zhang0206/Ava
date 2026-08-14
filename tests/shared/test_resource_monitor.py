"""Unit tests for shared.resource_monitor."""

from shared.resource_monitor import ResourceCollector, resource_snapshot


class TestResourceCollector:
    def test_initial_snapshot_returns_one_point(self):
        c = ResourceCollector(max_samples=10)
        data = c.snapshot()
        assert len(data) == 1
        p = data[0]
        assert p.ts > 0
        assert isinstance(p.cpu_pct, (int, float))
        assert p.mem_used_gb >= 0
        assert p.mem_total_gb > 0
        assert p.disk_used_gb >= 0
        assert p.disk_total_gb > 0

    def test_multiple_snapshots_accumulate(self):
        c = ResourceCollector(max_samples=10)
        for _ in range(5):
            c.snapshot()
        data = c.snapshot()
        assert len(data) == 6

    def test_buffer_bounded(self):
        c = ResourceCollector(max_samples=3)
        for _ in range(5):
            c.snapshot()
        data = c.snapshot()
        assert len(data) == 3  # clamped to max_samples

    def test_timestamps_monotonic(self):
        c = ResourceCollector(max_samples=5)
        for _ in range(3):
            c.snapshot()
        data = c.snapshot()
        for i in range(1, len(data)):
            assert data[i].ts >= data[i - 1].ts

    def test_displayed_ratio_matches_percent(self):
        # Regression: the shown used/total pair must agree with the shown
        # percent. psutil's `.total` (disk) and `.used` (mem) use a different
        # denominator than `.percent`, which made disk read 69% next to an 84%
        # bar and memory 34% next to 80%. The collector now derives used/total
        # from the same denominator, so used/total*100 ≈ percent.
        c = ResourceCollector(max_samples=1)
        p = c.snapshot()[0]
        disk_ratio = 100 * p.disk_used_gb / p.disk_total_gb
        mem_ratio = 100 * p.mem_used_gb / p.mem_total_gb
        assert abs(disk_ratio - p.disk_pct) < 1.5, (disk_ratio, p.disk_pct)
        assert abs(mem_ratio - p.mem_pct) < 1.5, (mem_ratio, p.mem_pct)

    def test_fields_present(self):
        c = ResourceCollector(max_samples=1)
        data = c.snapshot()
        p = data[0]
        assert set(type(p).model_fields) == {
            "ts",
            "cpu_pct",
            "mem_used_gb",
            "mem_total_gb",
            "mem_pct",
            "disk_used_gb",
            "disk_total_gb",
            "disk_pct",
        }


class TestModuleLevelSnapshot:
    def test_returns_list(self):
        data = resource_snapshot()
        assert isinstance(data, list)
        assert len(data) >= 1
