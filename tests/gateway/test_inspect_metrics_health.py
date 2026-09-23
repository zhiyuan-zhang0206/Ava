"""Coverage-note routing for the inspector metrics read model (task #3869).

Expected limits (historical coverage, compact boundary, legacy archive
precision) log — cooldown-deduped — and never open an instance. An unexpected
limit (``missing_turn_durations`` on a window inside the collection era) opens
one alerts-store episode through the standard writers; both its edges derive
from the store, so a later read — or a restarted process — resolves a cleared
condition while a re-fire reuses the stored instance. The note is best-effort:
it must never raise into the statistics read path.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from gateway import inspect_metrics_health as imh
from gateway.schemas.inspect_metrics import InspectMetricsMetadata, MetricEvidence

T0 = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)  # collection started
T2 = datetime(2026, 9, 17, 13, 0, tzinfo=UTC)


def _ev(
    availability: str = "observed",
    reason: str | None = None,
    retained: tuple[str, ...] = (),
) -> MetricEvidence:
    return MetricEvidence(
        availability=availability,  # pyright: ignore[reportArgumentType]
        sources=["observations"],
        reason=reason,  # pyright: ignore[reportArgumentType]
        retained_unapplied_sources=list(retained),
    )


def _md(
    *,
    cost: MetricEvidence | None = None,
    turns: MetricEvidence | None = None,
    activity: MetricEvidence | None = None,
    lifecycle: MetricEvidence | None = None,
    window_start: datetime | None = T0,
) -> InspectMetricsMetadata:
    return InspectMetricsMetadata(
        window_start=window_start,
        window_end=T2,
        sampled_at=T2,
        collection_started_at=T1,
        last_observed_at=T2,
        cost=cost or _ev(),
        turns=turns or _ev(),
        activity=activity or _ev(),
        lifecycle=lifecycle or _ev(),
    )


class _SpyPool:
    """Raises if anything tries to open a connection (the note must swallow it)."""

    def __init__(self) -> None:
        self.used = False

    @contextmanager
    def connection(self, timeout: float | None = None) -> Generator[Any, None, None]:
        self.used = True
        raise AssertionError("the DB must not be touched for expected coverage limits")
        yield  # pragma: no cover


class _ConnPool:
    """Stub pool over one live test connection (reads and writes borrow it)."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self.borrows = 0

    @contextmanager
    def connection(self, timeout: float | None = None) -> Generator[Any, None, None]:
        self.borrows += 1
        yield self.conn


@pytest.fixture(autouse=True)
def _reset_health_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imh, "_last_logged", {})
    monkeypatch.setattr(imh.settings.alerts, "im_notify_enabled", False)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr("gateway.routers.alerts.publish_alert_rows", rows.extend)
    return rows


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    texts: list[str] = []

    def _fake_notify(text: str) -> bool:
        texts.append(text)
        return True

    monkeypatch.setattr(imh, "notify_im", _fake_notify)
    return texts


def _coverage_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if "coverage limit" in str(r["message"])]


def _alerts_rows(conn: psycopg.Connection, agent_id: int = 42) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT status, severity, alertname, source, labels, notified_at, ends_at, starts_at "
        "FROM alerts WHERE source = 'inspect-metrics' AND labels->>'agent_id' = %s "
        "ORDER BY starts_at",
        (str(agent_id),),
    ).fetchall()


# ── expected limits: log once (cooldown), open no instance ────────────────────


def test_expected_limit_logs_once_and_opens_no_instance(
    db_conn: psycopg.Connection,
    published: list[dict[str, Any]],
    sent: list[str],
    loguru_records: list[dict[str, Any]],
) -> None:
    pool = _ConnPool(db_conn)
    md = _md(cost=_ev("partial", "historical_coverage_unknown"))
    note = imh.note_inspect_metrics_coverage
    note(pool, 42, md, spawned_at=T0)  # window reaches before collection -> historical
    note(pool, 42, md, spawned_at=T0)  # within cooldown: deduped
    records = _coverage_records(loguru_records)
    assert len(records) == 1
    extra = records[0]["extra"]
    assert extra["condition"] == "historical_coverage_unknown"
    assert extra["family"] == "cost"
    assert extra["expected"] is True
    assert _alerts_rows(db_conn) == []  # log-only: no instance, no notification
    assert published == [] and sent == []
    # One bounded store read per call (the resolve reconcile), nothing else.
    assert pool.borrows == 2


def test_cooldown_elapsed_logs_again(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(imh, "_cooldown_seconds", lambda: 0.0)
    md = _md(lifecycle=_ev("partial", "historical_coverage_unknown"))
    pool = _ConnPool(db_conn)
    imh.note_inspect_metrics_coverage(pool, 42, md, spawned_at=T0)
    imh.note_inspect_metrics_coverage(pool, 42, md, spawned_at=T0)
    assert len(_coverage_records(loguru_records)) == 2


# ── condition classification ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("evidence", "condition"),
    [
        (_ev("partial", "historical_coverage_unknown"), "historical_coverage_unknown"),
        (_ev("partial", "missing_turn_durations"), "missing_turn_durations"),
        (_ev("partial", "archive_precision_unattributed"), "archive_precision_unattributed"),
        (
            _ev("partial", None, retained=("historical_archive_distribution",)),
            "archive_precision_unattributed",
        ),
        # missing durations stays primary when the retained-source caveat rides along
        (
            _ev("partial", "missing_turn_durations", retained=("historical_archive_distribution",)),
            "missing_turn_durations",
        ),
    ],
)
def test_condition_classification(evidence: MetricEvidence, condition: str) -> None:
    assert imh._conditions(_md(turns=evidence)) == [("turns", condition, evidence.availability)]


# ── unexpected limit: store episode, resolve on clear ─────────────────────────


def test_unexpected_missing_durations_opens_episode_and_resolves(
    db_conn: psycopg.Connection,
    published: list[dict[str, Any]],
    sent: list[str],
) -> None:
    pool = _ConnPool(db_conn)
    # Window starts inside the collection era -> the gap is live (unexpected).
    bad = _md(turns=_ev("partial", "missing_turn_durations"), window_start=T1)
    note = imh.note_inspect_metrics_coverage
    note(pool, 42, bad, spawned_at=T0)

    rows = _alerts_rows(db_conn)
    assert len(rows) == 1
    status, severity, alertname, source, labels, notified_at, ends_at, _starts = rows[0]
    assert (status, severity, source) == ("unresolved", "warning", "inspect-metrics")
    assert alertname == "inspect metrics coverage"
    assert labels["condition"] == "missing_turn_durations" and labels["family"] == "turns"
    assert notified_at is not None and ends_at is None
    assert len(published) == 1 and len(sent) == 1
    assert "limited by missing_turn_durations" in sent[0]

    # Same read within cooldown: no duplicate row, no duplicate IM.
    note(pool, 42, bad, spawned_at=T0)
    assert len(_alerts_rows(db_conn)) == 1
    assert len(sent) == 1

    # A later read without the condition resolves the episode.
    note(pool, 42, _md(window_start=T1), spawned_at=T0)
    rows = _alerts_rows(db_conn)
    assert len(rows) == 1 and rows[0][0] == "resolved" and rows[0][6] is not None
    assert len(sent) == 2 and "recovered from missing_turn_durations" in sent[1]


def test_restart_never_strands_or_duplicates_a_stored_episode(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    published: list[dict[str, Any]],
    sent: list[str],
) -> None:
    """#2790 review probe: the old in-process episode map stranded rows on a
    cold process — re-fire must reuse the stored instance and a cleared
    condition must resolve it with no in-process memory of the firing."""
    pool = _ConnPool(db_conn)
    bad = _md(turns=_ev("partial", "missing_turn_durations"), window_start=T1)
    note = imh.note_inspect_metrics_coverage
    note(pool, 42, bad, spawned_at=T0)
    starts_at = _alerts_rows(db_conn)[0][7]

    # A gateway restart loses every in-process trace of the episode.
    monkeypatch.setattr(imh, "_last_logged", {})
    monkeypatch.setattr(imh, "_cooldown_seconds", lambda: 0.0)

    # Re-fire (still broken): reuses the stored instance — no duplicate row,
    # no new notification, the original starts_at.
    note(pool, 42, bad, spawned_at=T0)
    rows = _alerts_rows(db_conn)
    assert len(rows) == 1 and rows[0][7] == starts_at
    assert len(sent) == 1

    # Condition cleared on the cold process: the store-derived reconcile
    # resolves the instance the in-process map alone would have stranded.
    note(pool, 42, _md(window_start=T1), spawned_at=T0)
    rows = _alerts_rows(db_conn)
    assert len(rows) == 1 and rows[0][0] == "resolved" and rows[0][6] is not None
    assert len(sent) == 2 and "recovered from missing_turn_durations" in sent[1]


def test_condition_still_present_is_not_resolved_by_the_reconcile(
    db_conn: psycopg.Connection,
    published: list[dict[str, Any]],
    sent: list[str],
) -> None:
    pool = _ConnPool(db_conn)
    bad = _md(turns=_ev("partial", "missing_turn_durations"), window_start=T1)
    imh.note_inspect_metrics_coverage(pool, 42, bad, spawned_at=T0)
    imh.note_inspect_metrics_coverage(pool, 42, bad, spawned_at=T0)  # still broken
    rows = _alerts_rows(db_conn)
    assert len(rows) == 1 and rows[0][0] == "unresolved"


def test_note_never_raises_into_the_read_path(loguru_records: list[dict[str, Any]]) -> None:
    bad = _md(turns=_ev("partial", "missing_turn_durations"), window_start=T1)
    imh.note_inspect_metrics_coverage(_SpyPool(), 42, bad, spawned_at=T0)  # DB blows up
    assert any("note failed" in str(r["message"]) for r in loguru_records)
