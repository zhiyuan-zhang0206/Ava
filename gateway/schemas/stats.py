"""The stats window vocabulary shared by the dashboard, inspector, and fleet
reads: the whitelisted `?hours=` values and their resolution to a duration.
"""

from datetime import timedelta
from enum import IntEnum


class StatsWindowHours(IntEnum):
    """Whitelisted `?hours=` windows for stats, inspect, and fleet — 0 = last
    5 minutes / 1h / 6h / 24h / 3d / 7d. An enum (not `int` + range check)
    so FastAPI 422s any other value instead of silently aggregating an arbitrary window.
    (A `Literal[1, 6, ...]` won't do: query params arrive as strings and
    int-literal validation doesn't coerce them, 422ing even valid values.)"""

    M5 = 0
    H1 = 1
    H6 = 6
    H24 = 24
    D3 = 72
    D7 = 168


def window_delta(hours: StatsWindowHours) -> timedelta:
    """Resolve the `?hours=` wire value to its actual aggregation duration."""
    return timedelta(minutes=5) if hours == StatsWindowHours.M5 else timedelta(hours=int(hours))
