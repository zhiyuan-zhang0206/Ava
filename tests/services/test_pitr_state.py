import pytest

from services.pitr.state import health_state


def test_local_archive_never_counts_as_remote_ack() -> None:
    state = health_state(
        local_segments=3,
        local_bytes=48,
        remote_segments=0,
        remote_bytes=0,
        oldest_unacked_seconds=30,
        last_remote_ack_lsn=None,
        archive_errors_total=0,
        quota_rejections_total=0,
        upload_errors_total=0,
        warn_bytes=32,
        hard_bytes=64,
        warn_seconds=3600,
        critical_seconds=7200,
    )
    assert state.local_archived_segments == 3
    assert state.remote_acked_segments == 0
    assert state.unacked_segments == 3
    assert state.level == "degraded"


def test_hard_bound_reports_pg_wal_retention_risk() -> None:
    state = health_state(
        local_segments=4,
        local_bytes=64,
        remote_segments=0,
        remote_bytes=0,
        oldest_unacked_seconds=120,
        last_remote_ack_lsn=None,
        archive_errors_total=1,
        quota_rejections_total=1,
        upload_errors_total=2,
        warn_bytes=32,
        hard_bytes=64,
        warn_seconds=3600,
        critical_seconds=7200,
    )
    assert state.level == "critical"
    assert state.detail is not None and "pg_wal" in state.detail


def test_remote_ack_cannot_exceed_local_archive() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        health_state(
            local_segments=0,
            local_bytes=0,
            remote_segments=1,
            remote_bytes=16,
            oldest_unacked_seconds=None,
            last_remote_ack_lsn="0/1",
            archive_errors_total=0,
            quota_rejections_total=0,
            upload_errors_total=0,
            warn_bytes=32,
            hard_bytes=64,
            warn_seconds=3600,
            critical_seconds=7200,
        )
