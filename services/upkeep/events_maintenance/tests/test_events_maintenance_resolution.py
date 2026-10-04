"""Regression coverage for immutable event-class resolution (task #1468)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from services.upkeep.events_maintenance import resolution
from services.upkeep.events_maintenance.tests.slices import events_maintenance_config


class _Pool:
    """Minimal pool seam over the per-test real Postgres connection."""

    class _BorrowedConnection:
        def __init__(self, conn: psycopg.Connection[Any]) -> None:
            self._conn = conn

        def __enter__(self) -> psycopg.Connection[Any]:
            return self._conn

        def __exit__(self, *_args: object) -> None:
            return None

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def connection(self) -> _Pool._BorrowedConnection:
        return self._BorrowedConnection(self._conn)


def _event_class(
    *,
    category: str = "telemetry",
    level: str = "warning",
    event_name: str = "x",
    source: str = "test",
    process: str = "",
) -> resolution.EventClass:
    return resolution.EventClass(
        category=category, level=level, event_name=event_name, source=source, process=process
    )


def _insert_dismissal(conn: psycopg.Connection[Any], event_class: resolution.EventClass) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event_dismissals
                (category, level, event_name, source, process, dismissed_by)
            VALUES (%s, %s, %s, %s, %s, 0)
            """,
            (
                event_class.category,
                event_class.level,
                event_class.event_name,
                event_class.source,
                event_class.process,
            ),
        )
    conn.commit()


@pytest.fixture(autouse=True)
def _clear_dismissals(db_conn: psycopg.Connection[Any]) -> None:
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE event_dismissals")
    db_conn.commit()
    _fresh(db_conn)


def _fake_counts(
    *, unresolved: dict[resolution.EventClass, int], burst: dict[resolution.EventClass, int]
) -> Any:
    """A `class_counts` stand-in that answers the six-hour and the ten-minute window."""

    def class_counts(
        _conn: object, *, start: datetime, end: datetime, cluster: str | None = None
    ) -> dict[resolution.EventClass, int]:
        assert cluster is None
        return unresolved if end - start == timedelta(hours=6) else burst

    return class_counts


def _fresh(conn: psycopg.Connection[Any]) -> None:
    """One recent row, so the record reads as current."""
    _record(conn, level="info", event_name="heartbeat", minutes_ago=0.1)


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, object]]]:
    emitted: list[tuple[str, str, dict[str, object]]] = []

    def emit(category: str, event_name: str, **kwargs: object) -> None:
        emitted.append((category, event_name, cast(dict[str, object], kwargs["attributes"])))

    monkeypatch.setattr(resolution.telemetry, "emit", emit)
    return emitted


def _record(
    conn: psycopg.Connection[Any],
    *,
    minutes_ago: float = 1,
    level: str = "warning",
    event_name: str = "x",
    source: str = "test",
    process: str = "p",
    cluster: str = "c",
    category: str = "telemetry",
) -> None:
    conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
        "event_name, level, source) VALUES (%s, %s, 'm', %s, %s, %s, %s, %s, %s)",
        (
            uuid.uuid4().int % (1 << 62),
            datetime.now(UTC) - timedelta(minutes=minutes_ago),
            cluster,
            process,
            category,
            event_name,
            level,
            source,
        ),
    )
    conn.commit()


def _window(minutes: float) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now - timedelta(minutes=minutes), now


def test_class_counts_group_the_warning_error_and_critical_rows_of_the_window(
    db_conn: psycopg.Connection[Any],
) -> None:
    _record(db_conn, level="warning", event_name="a")
    _record(db_conn, level="warning", event_name="a")
    _record(db_conn, level="critical", event_name="b", process="q", category="log")
    _record(db_conn, level="info", event_name="a")  # not a problem level
    _record(db_conn, level="error", event_name="old", minutes_ago=400)  # outside the window

    start, end = _window(360)
    counts = resolution.class_counts(db_conn, start=start, end=end)

    assert counts == {
        _event_class(event_name="a", process="p"): 2,
        _event_class(level="critical", event_name="b", process="q", category="log"): 1,
    }


def test_alert_classes_tally_one_class_per_identity_with_first_and_last_seen(
    db_conn: psycopg.Connection[Any],
) -> None:
    _record(db_conn, event_name="a", minutes_ago=50, category="log")
    _record(db_conn, event_name="a", minutes_ago=10, category="telemetry")
    _record(db_conn, event_name="a", minutes_ago=5)
    _record(db_conn, event_name="a", process="q", minutes_ago=3)
    _record(db_conn, level="critical", event_name="b", minutes_ago=2)
    _record(db_conn, level="info", event_name="a")  # not a problem level
    _record(db_conn, event_name="old", minutes_ago=400)  # outside the window

    start, end = _window(360)
    classes = resolution.alert_classes(db_conn, start=start, end=end)

    # The emission category is not part of the identity: the log and telemetry rows of "a" are
    # one class, tallied together, carrying the greater category for a dismissal made from it.
    assert [(c.level, c.event_name, c.process, c.category, c.count) for c in classes] == [
        ("warning", "a", "p", "telemetry", 3),
        ("warning", "a", "q", "telemetry", 1),
        ("critical", "b", "p", "telemetry", 1),
    ]
    first = classes[0]
    assert (first.last_seen - first.first_seen).total_seconds() == pytest.approx(45 * 60, abs=1)


def test_alert_classes_cluster_filter_keeps_the_home_cluster_and_unlabelled_rows(
    db_conn: psycopg.Connection[Any],
) -> None:
    _record(db_conn, cluster="home", event_name="a")
    _record(db_conn, cluster="", event_name="b")
    _record(db_conn, cluster="elsewhere", event_name="c")

    start, end = _window(10)

    home = resolution.alert_classes(db_conn, start=start, end=end, cluster="home")
    everything = resolution.alert_classes(db_conn, start=start, end=end)
    assert {c.event_name for c in home} == {"a", "b"}
    assert {c.event_name for c in everything} == {"a", "b", "c"}


def test_matching_dismissal_prefers_the_exact_process_row_over_the_wildcard() -> None:
    wildcard = resolution.Dismissal(
        id=1, event_class=_event_class(process=""), dismissed_by=0, note=""
    )
    exact = resolution.Dismissal(
        id=2, event_class=_event_class(process="agent-host"), dismissed_by=0, note=""
    )
    other_name = resolution.Dismissal(
        id=3, event_class=_event_class(event_name="other"), dismissed_by=0, note=""
    )
    active = [wildcard, exact, other_name]

    assert resolution.matching_dismissal(_event_class(process="agent-host"), active) is exact
    assert resolution.matching_dismissal(_event_class(process="gateway"), active) is wildcard
    # A counted class with no process matches only the wildcard, never an exact row.
    assert resolution.matching_dismissal(_event_class(process=""), [exact]) is None
    # The emission category never takes part in the match.
    assert (
        resolution.matching_dismissal(_event_class(category="log", process="gateway"), active)
        is wildcard
    )
    assert resolution.matching_dismissal(_event_class(event_name="unseen"), active) is None


def test_unresolved_math_excludes_active_classes(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    dismissed_warning = _event_class(event_name="dismissed-warning")
    dismissed_error = _event_class(level="error", event_name="dismissed-error")
    remaining_warning = _event_class(event_name="remaining-warning")
    remaining_critical = _event_class(level="critical", event_name="remaining-critical")
    _insert_dismissal(db_conn, dismissed_warning)
    _insert_dismissal(db_conn, dismissed_error)
    counts = {
        dismissed_warning: 7,
        dismissed_error: 3,
        remaining_warning: 2,
        remaining_critical: 4,
    }

    monkeypatch.setattr(resolution, "class_counts", _fake_counts(unresolved=counts, burst={}))
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(
        cast(ConnectionPool, _Pool(db_conn)), events_maintenance_config()
    )

    assert result == resolution.ResolutionResult(2, 4, reopened=0, auto_dismissed=0)
    assert emitted == [
        (
            "telemetry",
            "resolution_status",
            {
                "unresolved_warnings": 2,
                "unresolved_errors": 4,
                "dismissed_warnings": 7,
                "dismissed_errors": 3,
                "window": "6h",
            },
        )
    ]


def test_burst_reopens_only_above_the_configured_threshold(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    hit = _event_class(event_name="hit")
    below = _event_class(event_name="below")
    _insert_dismissal(db_conn, hit)
    _insert_dismissal(db_conn, below)
    counts = {hit: 6, below: 5}

    monkeypatch.setattr(resolution, "class_counts", _fake_counts(unresolved=counts, burst=counts))
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(
        cast(ConnectionPool, _Pool(db_conn)),
        events_maintenance_config(events_resolution_burst_threshold=5),
    )

    assert result == resolution.ResolutionResult(6, 0, reopened=1, auto_dismissed=0)
    assert emitted[0] == (
        "telemetry",
        "warning_reopened",
        {
            "category": "telemetry",
            "level": "warning",
            "event_name": "hit",
            "source": "test",
            "process": "",
            "agent_id": None,
            "dismissed_by": 0,
            "note": "auto:burst",
            "reopened_by": "system:burst",
            "triggered_by_count": 6,
        },
    )
    assert emitted[-1][2]["unresolved_warnings"] == 6
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT event_name, status, burst_count FROM event_dismissals ORDER BY event_name"
        )
        assert cur.fetchall() == [("below", "dismissed", None), ("hit", "reopened", 6)]


def test_a_stale_record_or_a_failed_read_never_emits_a_gauge(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted = _capture_events(monkeypatch)
    pool = cast(ConnectionPool, _Pool(db_conn))

    # The newest recorded event is an hour old: an empty window may only mean a stalled writer.
    later = datetime.now(UTC) + timedelta(hours=1)
    assert resolution.run_resolution_slice(pool, events_maintenance_config(), now=later) is None
    assert emitted == []

    def boom(*_args: object, **_kwargs: object) -> dict[resolution.EventClass, int]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(resolution, "class_counts", boom)
    assert resolution.run_resolution_slice(pool, events_maintenance_config()) is None
    assert emitted == []


def test_an_empty_window_over_a_current_record_emits_zero_gauges(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(
        cast(ConnectionPool, _Pool(db_conn)), events_maintenance_config()
    )

    assert result == resolution.ResolutionResult(0, 0, reopened=0, auto_dismissed=0)
    assert [name for _category, name, _attrs in emitted] == ["resolution_status"]


def test_auto_dismiss_picks_the_classes_present_in_every_six_hour_slice(
    db_conn: psycopg.Connection[Any],
) -> None:
    for slot in range(4):  # one day of six-hour slices
        _record(db_conn, event_name="steady", minutes_ago=slot * 360 + 30)
        if slot != 2:
            _record(db_conn, event_name="gappy", minutes_ago=slot * 360 + 30)
    now = datetime.now(UTC)
    current = {
        _event_class(event_name="steady", process="p"): 1,
        _event_class(event_name="gappy", process="p"): 1,
    }
    config = events_maintenance_config(events_auto_dismiss_enabled=True, events_auto_dismiss_days=1)
    resolution._last_auto_dismiss_day[0] = None

    stable = resolution._stable_auto_classes(now, db_conn, current, config)

    assert stable == {_event_class(event_name="steady", process="p")}


def test_auto_dismiss_is_off_by_default(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    stable = _event_class(event_name="stable-warning")

    monkeypatch.setattr(
        resolution, "class_counts", _fake_counts(unresolved={stable: 1}, burst={stable: 1})
    )
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(
        cast(ConnectionPool, _Pool(db_conn)),
        events_maintenance_config(events_auto_dismiss_enabled=False),
    )

    assert result == resolution.ResolutionResult(1, 0, reopened=0, auto_dismissed=0)
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM event_dismissals")
        assert cur.fetchone() == (0,)
    assert [event_name for _category, event_name, _attrs in emitted] == ["resolution_status"]


def test_level_splits_three_way_arithmetic() -> None:
    """The shared window-agnostic split: total / dismissed / net per level,
    with critical folding into error (task #1935)."""
    dismissed_warning = _event_class(event_name="dismissed-warning")
    remaining_warning = _event_class(event_name="remaining-warning")
    dismissed_error = _event_class(level="error", event_name="dismissed-error")
    remaining_critical = _event_class(level="critical", event_name="remaining-critical")
    counts = {
        dismissed_warning: 7,
        remaining_warning: 2,
        dismissed_error: 3,
        remaining_critical: 4,
    }
    active = {dismissed_warning, dismissed_error}

    splits = resolution.level_splits(counts, active)

    assert splits == {
        "warning": resolution.LevelSplit(total=9, dismissed=7, net=2),
        "error": resolution.LevelSplit(total=7, dismissed=3, net=4),
    }
    assert splits["warning"].total == splits["warning"].dismissed + splits["warning"].net
    assert splits["error"].total == splits["error"].dismissed + splits["error"].net


def test_level_splits_empty_and_unknown_levels() -> None:
    """No counts -> no levels; a level outside warning/error still buckets
    by the same rule (anything non-warning is the error family)."""
    assert resolution.level_splits({}, set()) == {}
    odd = _event_class(level="critical", event_name="c")
    splits = resolution.level_splits({odd: 5}, set())
    assert splits == {"error": resolution.LevelSplit(total=5, dismissed=0, net=5)}


def test_daemon_emits_dismissed_gauges_alongside_unresolved(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolution_status carries the dismissed counts so Grafana can render
    the total / resolved / net trio (task #1935)."""
    dismissed = _event_class(event_name="dismissed")
    _insert_dismissal(db_conn, dismissed)
    counts = {dismissed: 4, _event_class(event_name="kept"): 1}

    monkeypatch.setattr(resolution, "class_counts", _fake_counts(unresolved=counts, burst={}))
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(
        cast(ConnectionPool, _Pool(db_conn)), events_maintenance_config()
    )

    assert result == resolution.ResolutionResult(1, 0, reopened=0, auto_dismissed=0)
    status_event = next(
        (category, name, attrs) for category, name, attrs in emitted if name == "resolution_status"
    )
    assert status_event[2] == {
        "unresolved_warnings": 1,
        "unresolved_errors": 0,
        "dismissed_warnings": 4,
        "dismissed_errors": 0,
        "window": "6h",
    }


def test_process_scopes_dismissal_matching() -> None:
    """Exact rows cancel one process; wildcard rows cancel every process; a
    mixed-version counted class (no process in its body) matches only the
    wildcard (task #4329 B5)."""

    agent_host = _event_class(process="agent_host")
    im_bridge = _event_class(process="im_bridge")
    legacy = _event_class()  # pre-dimension body: no process -> ""
    counts = {agent_host: 3, im_bridge: 3, legacy: 1}

    exact = resolution.level_splits(counts, {agent_host})
    assert exact["warning"] == resolution.LevelSplit(total=7, dismissed=3, net=4)

    wildcard = resolution.level_splits(counts, {_event_class()})
    assert wildcard["warning"] == resolution.LevelSplit(total=7, dismissed=7, net=0)


def test_exact_dismissal_reopens_only_on_its_own_process_burst(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its own process's burst reopens an exact dismissal; a different
    process's burst of the same base must not (task #4329 B5)."""

    host_class = _event_class(process="agent_host")
    bridge_class = _event_class(process="im_bridge")
    _insert_dismissal(db_conn, host_class)
    _insert_dismissal(db_conn, bridge_class)
    counts = {host_class: 6, bridge_class: 2}

    monkeypatch.setattr(resolution, "class_counts", _fake_counts(unresolved=counts, burst=counts))
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(
        cast(ConnectionPool, _Pool(db_conn)),
        events_maintenance_config(events_resolution_burst_threshold=5),
    )

    assert result == resolution.ResolutionResult(6, 0, reopened=1, auto_dismissed=0)
    assert emitted[0][1] == "warning_reopened"
    assert emitted[0][2]["process"] == "agent_host"
    with db_conn.cursor() as cur:
        cur.execute("SELECT event_name, process, status FROM event_dismissals ORDER BY process")
        assert cur.fetchall() == [
            ("x", "agent_host", "reopened"),
            ("x", "im_bridge", "dismissed"),
        ]


def test_wildcard_dismissal_reopens_on_the_whole_base_burst(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wildcard (process="") dismissal — every pre-dimension row's scope —
    watches the summed ten-minute count across processes."""

    _insert_dismissal(db_conn, _event_class())  # process="" wildcard
    counts = {_event_class(process="agent_host"): 4, _event_class(process="im_bridge"): 2}

    monkeypatch.setattr(resolution, "class_counts", _fake_counts(unresolved=counts, burst=counts))
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(
        cast(ConnectionPool, _Pool(db_conn)),
        events_maintenance_config(events_resolution_burst_threshold=5),
    )

    # 4 + 2 = 6 > 5: the base-wide burst trips the wildcard row's safety valve.
    assert result == resolution.ResolutionResult(6, 0, reopened=1, auto_dismissed=0)
    assert emitted[0][2]["process"] == ""
    assert emitted[0][2]["triggered_by_count"] == 6


def test_dismissal_matching_ignores_the_emission_category() -> None:
    """A row stored from an event's log-era life cancels the same class under
    its telemetry life, and vice versa (the 2026-08 log->telemetry
    reclassification left whole rows inert before this; task #4964)."""

    telemetry = _event_class(category="telemetry", event_name="sse_drop")
    log_row = _event_class(category="log", event_name="sse_drop")

    splits = resolution.level_splits({telemetry: 5}, {log_row})
    assert splits["warning"] == resolution.LevelSplit(total=5, dismissed=5, net=0)

    splits = resolution.level_splits({log_row: 5}, {telemetry})
    assert splits["warning"] == resolution.LevelSplit(total=5, dismissed=5, net=0)


def test_category_agnostic_matching_keeps_the_process_rules() -> None:
    """Dropping category does not loosen the process scope: an exact row
    cancels only its process; a wildcard row cancels every process."""

    exact = _event_class(category="log", process="agent_host")
    same_process = _event_class(category="telemetry", process="agent_host")
    other_process = _event_class(category="telemetry", process="im_bridge")
    counts = {same_process: 3, other_process: 3}

    scoped = resolution.level_splits(counts, {exact})
    assert scoped["warning"] == resolution.LevelSplit(total=6, dismissed=3, net=3)

    wildcard = resolution.level_splits(counts, {_event_class(category="log")})
    assert wildcard["warning"] == resolution.LevelSplit(total=6, dismissed=6, net=0)


def test_burst_valve_watches_the_base_across_the_category_split(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wildcard row stored from log-era life reopens on the telemetry burst
    of its base (task #4964)."""

    _insert_dismissal(db_conn, _event_class(category="log", event_name="sse_drop"))
    counts = {_event_class(category="telemetry", event_name="sse_drop"): 6}

    monkeypatch.setattr(resolution, "class_counts", _fake_counts(unresolved=counts, burst=counts))
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(
        cast(ConnectionPool, _Pool(db_conn)),
        events_maintenance_config(events_resolution_burst_threshold=5),
    )

    assert result == resolution.ResolutionResult(6, 0, reopened=1, auto_dismissed=0)
    assert emitted[0][1] == "warning_reopened"
    assert emitted[0][2]["triggered_by_count"] == 6


def test_exact_burst_valve_watches_its_process_across_the_category_split(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact row stored from log-era life reopens on its own process's
    telemetry burst — and not on another process's (task #4964)."""

    _insert_dismissal(db_conn, _event_class(category="log", process="agent_host"))
    _insert_dismissal(db_conn, _event_class(category="log", process="im_bridge"))
    counts = {
        _event_class(category="telemetry", process="agent_host"): 6,
        _event_class(category="telemetry", process="im_bridge"): 2,
    }

    monkeypatch.setattr(resolution, "class_counts", _fake_counts(unresolved=counts, burst=counts))
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(
        cast(ConnectionPool, _Pool(db_conn)),
        events_maintenance_config(events_resolution_burst_threshold=5),
    )

    assert result == resolution.ResolutionResult(6, 0, reopened=1, auto_dismissed=0)
    assert emitted[0][2]["process"] == "agent_host"
