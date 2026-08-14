"""Unit tests for shared.watcher — cron validation, next-fire, when
normalization, and watcher script generation (pure string builders)."""

import datetime as dt

import pytest

from shared.watcher import (
    CronExprError,
    build_at_script,
    build_cron_script,
    next_fire,
    normalize_end_time,
    normalize_when,
    validate_cron,
)


def test_validate_cron_accepts_valid() -> None:
    validate_cron("0 18 * * 1-5")  # no raise


def test_validate_cron_rejects_invalid() -> None:
    with pytest.raises(CronExprError):
        validate_cron("not a cron")


def test_next_fire_returns_future_utc() -> None:
    base = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    nxt = next_fire("0 13 * * *", after=base)
    assert nxt == dt.datetime(2026, 1, 1, 13, 0, tzinfo=dt.UTC)


def test_next_fire_tolerance_catches_late_wake() -> None:
    """A wake a few seconds past the fire minute must still resolve to the
    current period's fire — croniter's get_next is strictly > base, so without
    tolerance the next fire jumps a whole day (Task #958)."""
    # 13:00:05 — 5s past the 13:00 fire. No tolerance -> next day.
    late = dt.datetime(2026, 1, 1, 13, 0, 5, tzinfo=dt.UTC)
    nxt = next_fire("0 13 * * *", after=late)
    assert nxt == dt.datetime(2026, 1, 2, 13, 0, tzinfo=dt.UTC)
    # With 2min tolerance the current period's fire is returned (in the past
    # by 5s — caller treats wait <= threshold as fire-now).
    nxt_tol = next_fire("0 13 * * *", after=late, tolerance=dt.timedelta(minutes=2))
    assert nxt_tol == dt.datetime(2026, 1, 1, 13, 0, tzinfo=dt.UTC)


def test_next_fire_tolerance_does_not_shift_future_fires() -> None:
    """Tolerance only backs the base; a base well before the next fire is
    unchanged."""
    base = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    nxt = next_fire("0 13 * * *", after=base, tolerance=dt.timedelta(minutes=2))
    assert nxt == dt.datetime(2026, 1, 1, 13, 0, tzinfo=dt.UTC)


def test_next_fire_tolerance_timezone() -> None:
    """Tolerance composes with an explicit timezone."""
    late = dt.datetime(2026, 1, 1, 21, 0, 5, tzinfo=dt.UTC)  # 13:00:05 PT
    nxt = next_fire("0 13 * * *", after=late, timezone="America/Los_Angeles")
    assert nxt == dt.datetime(2026, 1, 2, 21, 0, tzinfo=dt.UTC)
    nxt_tol = next_fire(
        "0 13 * * *",
        after=late,
        timezone="America/Los_Angeles",
        tolerance=dt.timedelta(minutes=2),
    )
    assert nxt_tol == dt.datetime(2026, 1, 1, 21, 0, tzinfo=dt.UTC)


def test_normalize_when_naive_datetime_raises() -> None:
    with pytest.raises(ValueError, match="tzinfo"):
        normalize_when(dt.datetime(2026, 1, 1, 12, 0))  # noqa: DTZ001 — naive on purpose


def test_normalize_when_timedelta() -> None:
    out = normalize_when(dt.timedelta(minutes=5))
    assert out.tzinfo is not None


def test_normalize_when_iso_z() -> None:
    out = normalize_when("2026-01-01T12:00:00Z")
    assert out == dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)


def test_normalize_end_time_none() -> None:
    assert normalize_end_time(None) is None


def test_build_at_script_has_wake_and_sleep() -> None:
    when = dt.datetime(2030, 1, 1, 0, 0, tzinfo=dt.UTC)
    script = build_at_script(when_iso=when.isoformat(), message="hi there")
    # The wake helper delivers a watcher:N-tagged inbound to the launching agent.
    assert "def _wake" in script
    assert '"watcher:" + _os.environ["AVA_WATCHER_SESSION_ID"]' in script
    assert "_wake(_MESSAGE)" in script
    assert "time.sleep" in script
    assert "hi there" in script
    compile(script, "<at-script>", "exec")  # must be valid Python


def test_build_cron_script_has_loop_and_wake() -> None:
    script = build_cron_script(expr="0 * * * *", message="tick", timezone="UTC", end_time_iso=None)
    assert "while True" in script
    assert "def _wake" in script
    assert "_wake(_MESSAGE)" in script
    assert "0 * * * *" in script
    compile(script, "<cron-script>", "exec")


def test_cron_script_end_time_branch_does_not_wake() -> None:
    # end_time means "no more fires past this point". The branch that detects the
    # next fire is past _END must break silently — NOT emit a final duplicate
    # wake right after the last in-window fire.
    script = build_cron_script(
        expr="0 * * * *",
        message="tick",
        timezone="UTC",
        end_time_iso="2030-01-01T00:00:00+00:00",
    )
    # Isolate the `if _fire > _END:` block and assert it only breaks.
    lines = script.splitlines()
    guard_idx = next(i for i, ln in enumerate(lines) if "_fire > _END" in ln)
    # The guarded block runs until dedent back to loop-body indentation.
    block = lines[guard_idx + 1 :]
    body: list[str] = []
    for ln in block:
        if ln.strip() and not ln.startswith("        "):  # dedented out of the if-block
            break
        code_only = ln.split("#", 1)[0]  # ignore comments — they may mention "wake"
        body.append(code_only)
    block_text = "\n".join(body)
    assert "break" in block_text
    assert "_wake" not in block_text


# -- validate_timezone -------------------------------------------------------


def test_validate_timezone_accepts_valid() -> None:
    from shared.watcher import validate_timezone

    validate_timezone("UTC")
    validate_timezone("America/Los_Angeles")
    validate_timezone("Asia/Shanghai")
    validate_timezone("Europe/London")


def test_validate_timezone_rejects_invalid() -> None:
    from shared.watcher import validate_timezone

    with pytest.raises(ValueError, match="timezone"):
        validate_timezone("Not/A/Real/Timezone")
    with pytest.raises(ValueError):
        validate_timezone("")


# -- normalize_when edge cases -----------------------------------------------


def test_normalize_when_iso_with_offset() -> None:
    out = normalize_when("2026-01-01T12:00:00+08:00")
    assert out == dt.datetime(2026, 1, 1, 4, 0, tzinfo=dt.UTC)


def test_normalize_when_iso_with_microseconds() -> None:
    out = normalize_when("2026-01-01T12:00:00.123456Z")
    expected = dt.datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=dt.UTC)
    assert out == expected


def test_normalize_when_aware_datetime() -> None:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    dt_in = dt.datetime(2026, 7, 1, 8, 0, tzinfo=tz)
    out = normalize_when(dt_in)
    assert out == dt.datetime(2026, 7, 1, 15, 0, tzinfo=dt.UTC)  # July is PDT (UTC-7)


def test_normalize_when_iso_no_tz_raises() -> None:
    with pytest.raises(ValueError, match="timezone"):
        normalize_when("2026-01-01T12:00:00")


def test_normalize_when_bad_type_raises() -> None:
    with pytest.raises(TypeError, match="when must be"):
        normalize_when(42)  # type: ignore[arg-type]
