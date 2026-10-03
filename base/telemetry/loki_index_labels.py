"""Event-stream retention constants and Loki deploy-config pinning.

The Loki event readers and their index-label selector machinery were removed
with the 2026-10-03 telemetry-readers-on-postgres move (Loki is a discardable
projection; Postgres is the read surface). What remains are the constants
``validate_loki_deploy_config`` pins against the rendered native Loki config
at converge time, plus ``ARCHIVE_FREEZE_AT`` — the PG ``events`` archive's
freeze boundary read by events maintenance.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

EVENT_STREAM_SERVICE_NAME = "unknown_service"

# The PG `events` archive's freeze boundary (task #1281; the LGTM cutover,
# task #1197): `ARCHIVE_FREEZE_AT` is the archive's newest row with FULL
# microsecond precision (max(events.ts) = 2026-08-13 03:54:10.626517 UTC) —
# truncating to seconds would drop the archive rows within that trailing
# second (they are not in the live stream either). Events maintenance leaves
# rows at or before it to the archive.
ARCHIVE_FREEZE_AT = datetime(2026, 8, 13, 3, 54, 10, 626517, tzinfo=UTC)
EVENT_STREAM_RETENTION = timedelta(hours=84)
# Must match deployed Loki `querier.max_concurrent`; render validation catches
# drift before it ships (the 2026-08-18 incident). Raised 4 to 6 with the
# 2026-09-18 query-latency mitigation (task #3891) so queued queries drain
# faster, still bounded within the box's 10 cores.
LOKI_QUERY_CONCURRENCY = 6
# WAL disk-full write throttle (ingester.wal.disk_full_threshold, verified
# against loki 3.7.6 `-verify-config`): 0.95 tolerates the data volume's
# 89-91% oscillation while keeping a real disk-full guard.
WAL_DISK_FULL_THRESHOLD = 0.95
# Output series cap per query (limits_config.max_query_series). Sized for the
# events-maintenance rollup's daily turn-duration histogram, whose merged
# `sum by (agent_id, bucket)` shape legitimately returns one series per
# distinct (agent, integer-second bucket) — 3061 series for the busiest
# measured day (2026-08-24, 133 agents), so 2000 rejected the query and the
# whole rollup pass with it (2026-08-25). 20000 keeps ~6.5x headroom for
# fleet growth while still blocking pathological ad-hoc fan-outs (the
# upstream default is 500).
LOKI_MAX_QUERY_SERIES = 20000


def retention_hours() -> int:
    """Whole hours retained — the gateway's window clamp."""

    return int(EVENT_STREAM_RETENTION.total_seconds() // 3600)


def _retention_period_str() -> str:
    """Format the retention constant in Loki's whole-hour YAML syntax."""

    return f"{retention_hours()}h"


def validate_loki_deploy_config(config: Mapping[str, object]) -> None:
    """Reject rendered Loki retention, query-capacity, or WAL-throttle drift.

    Covers the global retention period, query capacity, and the WAL throttle. Every rendered native config passes through
    here at converge time (`cli/commands/observability/lgtm_native.py`)."""

    raw_limits_config = config["limits_config"]
    raw_querier = config["querier"]
    if not isinstance(raw_limits_config, Mapping) or not isinstance(raw_querier, Mapping):
        raise TypeError("Loki deploy config must contain limits_config and querier mappings")
    limits_config = cast(Mapping[str, object], raw_limits_config)
    querier = cast(Mapping[str, object], raw_querier)
    retention_period = limits_config["retention_period"]
    if retention_period != _retention_period_str():
        raise ValueError(
            f"Loki retention_period must be {_retention_period_str()!r}, got {retention_period!r}"
        )
    max_query_series = limits_config["max_query_series"]
    if max_query_series != LOKI_MAX_QUERY_SERIES:
        raise ValueError(
            "Loki limits_config.max_query_series must be "
            f"{LOKI_MAX_QUERY_SERIES}, got {max_query_series!r}"
        )
    max_concurrent = querier["max_concurrent"]
    if max_concurrent != LOKI_QUERY_CONCURRENCY:
        raise ValueError(
            f"Loki querier.max_concurrent must be {LOKI_QUERY_CONCURRENCY}, got {max_concurrent!r}"
        )
    # WAL disk-full throttle pin (2026-08-25, Task #1626): the upstream default
    # (0.9) flapped against the ~90%-full data volume and dropped the audit
    # event stream. Must stay explicitly pinned here so a re-render cannot
    # silently fall back to the flapping default.
    raw_ingester = config["ingester"]
    if not isinstance(raw_ingester, Mapping):
        raise TypeError("Loki deploy config must contain an ingester mapping")
    ingester = cast(Mapping[str, object], raw_ingester)
    raw_wal = ingester["wal"]
    if not isinstance(raw_wal, Mapping):
        raise TypeError("Loki deploy config must contain ingester.wal mapping")
    wal = cast(Mapping[str, object], raw_wal)
    threshold = wal["disk_full_threshold"]
    if threshold != WAL_DISK_FULL_THRESHOLD:
        raise ValueError(
            "Loki ingester.wal.disk_full_threshold must be "
            f"{WAL_DISK_FULL_THRESHOLD!r}, got {threshold!r}"
        )
