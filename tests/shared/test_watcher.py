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
    script = build_at_script(when_iso=when.isoformat(), message="hi there", timezone="UTC")
    # The wake helper delivers a watcher:N-tagged inbound to the launching agent.
    assert "def _wake" in script
    assert '"watcher:" + _os.environ["AVA_WATCHER_SESSION_ID"]' in script
    assert "_wake(_MESSAGE)" in script
    assert "time.sleep" in script
    assert "hi there" in script
    compile(script, "<at-script>", "exec")  # must be valid Python


def test_build_at_script_none_timezone_uses_host_wall_clock() -> None:
    """A settings-lite process has no authoritative cluster timezone: the
    one-shot announcement renders in the watcher process's own wall clock
    (the documented lite degradation), and the script needs no ZoneInfo."""

    when = dt.datetime(2030, 1, 1, 0, 0, tzinfo=dt.UTC)
    script = build_at_script(when_iso=when.isoformat(), message="hi there", timezone=None)
    assert "ZoneInfo" not in script
    assert "_WHEN.astimezone().isoformat()" in script
    assert "_WHEN.astimezone(_TZ).isoformat()" not in script
    compile(script, "<at-script>", "exec")  # must be valid Python


def test_build_at_script_announcement_uses_cluster_zone() -> None:
    """The one-shot startup announcement renders in the passed cluster
    timezone (user ruling 2026-08-27: one cluster clock), never the host OS
    zone — the sleep stays UTC-based regardless."""

    when = dt.datetime(2030, 1, 1, 0, 0, tzinfo=dt.UTC)
    script = build_at_script(
        when_iso=when.isoformat(), message="hi there", timezone="Asia/Shanghai"
    )
    assert "from zoneinfo import ZoneInfo" in script
    assert "_TZ = ZoneInfo('Asia/Shanghai')" in script
    assert "_WHEN.astimezone(_TZ).isoformat()" in script
    assert "astimezone().isoformat()" not in script  # no bare host-zone read
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


# -- wall-clock stepping (issue #182) -----------------------------------------


class _FakeWall:
    """A virtual wall clock whose `sleep` advances by the requested delay PLUS a
    per-sleep correction — the issue #182 mechanism (laptop resume / NTP steps
    the wall clock while a watcher sleeps, so waking up does not mean the clock
    reached the target). Corrections are consumed one per sleep call."""

    def __init__(self, start: dt.datetime, corrections: list[float]) -> None:
        self.t = start
        self.corrections = list(corrections)
        self.sleeps: list[float] = []

    def now(self) -> dt.datetime:
        return self.t

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.t += dt.timedelta(seconds=secs)
        if self.corrections:
            self.t += dt.timedelta(seconds=self.corrections.pop(0))
        if len(self.sleeps) > 500:
            raise RuntimeError("watcher loop did not converge on the fake clock")


class _FakeDateTime(dt.datetime):
    """Fake `datetime.datetime`: `now` reads the active fake wall clock (set per
    test). A subclass so croniter's issubclass check on its ret_type passes."""

    _wall: "_FakeWall | None" = None

    @staticmethod
    def now(_tz=None) -> dt.datetime:  # pyright: ignore[reportIncompatibleMethodOverride]
        assert _FakeDateTime._wall is not None
        return _FakeDateTime._wall.now()

    @classmethod
    def fromisoformat(cls, value: str) -> dt.datetime:
        return dt.datetime.fromisoformat(value)


class _FakeDT:
    """Mirrors the `datetime` module surface the generated scripts use."""

    UTC = dt.UTC
    timedelta = dt.timedelta
    datetime = _FakeDateTime


def _exec_watcher(
    script: str,
    wall: _FakeWall,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[object, object]]:
    """Execute a generated watcher script against the fake wall clock; return
    the (args, kwargs) of every `_wake` call.

    The generated scripts import `datetime` / `time` at the top; `datetime.datetime`
    is an immutable C type, so instead of patching it we substitute the two imports
    with fakes before exec — the template itself stays untouched. `_wake`'s delivery
    is stubbed through ava._gateway_client."""
    from ava import _boot, _gateway_client

    script = script.replace("import datetime as _dt\n", "_dt = _FakeDT\n").replace(
        "import time as _time\n", "_time = _fake_time\n"
    )
    fake_time = type("_FakeTime", (), {"sleep": staticmethod(wall.sleep)})()
    _FakeDateTime._wall = wall

    sent: list[tuple[object, object]] = []
    monkeypatch.setattr(_boot, "agent_id", lambda: 3115)
    monkeypatch.setattr(
        _gateway_client,
        "send_message",
        lambda *a, **k: sent.append((a, k)),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setenv("AVA_WATCHER_SESSION_ID", "77")
    exec(script, {"__name__": "__watcher__", "_FakeDT": _FakeDT, "_fake_time": fake_time})
    return sent


def test_cron_clock_step_backwards_never_fires_a_boundary_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue #182 scenario: every sleep lands the wall clock BACK before the
    boundary (accumulated ~1.5-3s corrections), and after a fire the clock steps
    back across the fired boundary. The watcher must fire each boundary exactly
    once — 3 fires for a */2 watcher with a 6-minute end_time, never an extra
    early fire and never a duplicate."""
    wall = _FakeWall(
        dt.datetime(2026, 8, 20, 23, 57, 58, tzinfo=dt.UTC),
        corrections=[-4.0, -5.0],
    )
    script = build_cron_script(
        expr="*/2 * * * *",
        message="tick",
        timezone="UTC",
        end_time_iso="2026-08-20T16:02:00-08:00",  # 00:02:00 UTC
    )
    sent = _exec_watcher(script, wall, monkeypatch)

    assert len(sent) == 3, f"expected exactly 3 boundary fires, got {len(sent)}"
    # Fires happened at the boundary instants — never before them.
    boundary_times = [
        dt.datetime(2026, 8, 20, 23, 58, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 21, 0, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 21, 0, 2, 0, tzinfo=dt.UTC),
    ]
    for (_args, _kwargs), boundary in zip(sent, boundary_times, strict=True):
        assert wall.t >= boundary, f"fired before the boundary ({wall.t} < {boundary})"


def test_at_clock_step_backwards_does_not_fire_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `at` watcher whose sleep lands the clock back before `when` (the 2.9s-early
    fire from the issue) must keep waiting and fire at/after `when`, exactly once."""
    when = dt.datetime(2026, 8, 20, 16, 0, 12, 330000, tzinfo=dt.UTC)
    wall = _FakeWall(
        dt.datetime(2026, 8, 20, 16, 0, 9, 420000, tzinfo=dt.UTC),  # 2.91s before when
        corrections=[-4.0],
    )
    script = build_at_script(when_iso=when.isoformat(), message="wake", timezone="UTC")
    sent = _exec_watcher(script, wall, monkeypatch)

    assert len(sent) == 1
    assert wall.t >= when, f"fired before `when` ({wall.t} < {when})"


def test_cron_clock_step_forward_still_fires_each_boundary_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clock stepping FORWARD (waking past the boundary) must not add or drop
    fires: each boundary fires once, late by the step but never twice."""
    wall = _FakeWall(
        dt.datetime(2026, 8, 20, 23, 57, 58, tzinfo=dt.UTC),
        corrections=[+3.0],
    )
    script = build_cron_script(
        expr="*/2 * * * *",
        message="tick",
        timezone="UTC",
        end_time_iso="2026-08-20T16:02:00-08:00",  # 00:02:00 UTC
    )
    sent = _exec_watcher(script, wall, monkeypatch)

    assert len(sent) == 3, f"expected exactly 3 boundary fires, got {len(sent)}"


def test_cron_script_stamps_template_version() -> None:
    """The generated cron script carries the template generation it was built
    from — the registry records it at spawn so the boot reconcile can rebuild
    live watchers whose script predates a template fix (issue #1330)."""
    script = build_cron_script(expr="0 * * * *", message="tick", timezone="UTC", end_time_iso=None)
    assert "_TEMPLATE_VERSION = 4" in script
    assert "TEMPLATE_VERSION" in __import__("shared.watcher", fromlist=["TEMPLATE_VERSION"]).__all__


def test_at_script_stamps_template_version() -> None:
    when = dt.datetime(2030, 1, 1, 0, 0, tzinfo=dt.UTC)
    script = build_at_script(when_iso=when.isoformat(), message="wake", timezone="UTC")
    assert "_TEMPLATE_VERSION = 4" in script


# -- schedule-state announcement (v3, task #1620) -----------------------------


def test_cron_script_announces_schedule_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A running cron watcher must say on stdout what it is doing: its next
    fire at startup and, after each fire, the fired instant plus the next fire.
    A healthy watcher sleeping toward a far boundary (a weekly cron a few
    minutes after its Monday fire) is otherwise indistinguishable from a stuck
    one — the 2026-08-25 false alarm that sent a working watcher to the
    chopping block."""
    wall = _FakeWall(
        dt.datetime(2026, 8, 20, 23, 57, 58, tzinfo=dt.UTC),
        corrections=[-4.0, -5.0],
    )
    script = build_cron_script(
        expr="*/2 * * * *",
        message="tick",
        timezone="UTC",
        end_time_iso="2026-08-20T16:02:00-08:00",  # 00:02:00 UTC
    )
    sent = _exec_watcher(script, wall, monkeypatch)
    out = capsys.readouterr().out

    assert len(sent) == 3  # fire semantics unchanged by the prints
    lines = [ln for ln in out.splitlines() if "[watcher]" in ln]
    # One announcement per upcoming fire: a startup line for the first fire,
    # then one line per fire naming the fired instant and the next fire. Each
    # line is printed before its sleep, so `next fire at` always names the
    # fire the loop is about to execute; after the last fire the loop breaks
    # at the end_time check and prints nothing more.
    assert len(lines) == 3
    assert "cron */2 * * * * in UTC -> next fire at 2026-08-20T23:58:00+00:00" in lines[0]
    assert "fired 2026-08-20T23:58:00+00:00 -> next fire at 2026-08-21T00:00:00+00:00" in lines[1]
    assert "fired 2026-08-21T00:00:00+00:00 -> next fire at 2026-08-21T00:02:00+00:00" in lines[2]


def test_at_script_announces_when(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A one-shot watcher announces its target time at startup, so a session
    capture shows at a glance when it will fire."""
    when = dt.datetime(2026, 8, 20, 16, 0, 0, tzinfo=dt.UTC)
    wall = _FakeWall(dt.datetime(2026, 8, 20, 15, 59, 30, tzinfo=dt.UTC), corrections=[])
    script = build_at_script(when_iso=when.isoformat(), message="wake", timezone="UTC")
    sent = _exec_watcher(script, wall, monkeypatch)
    out = capsys.readouterr().out

    assert len(sent) == 1  # fire semantics unchanged
    # Printed in the passed cluster timezone (user ruling 2026-08-27: one
    # cluster clock), matching the cron script's tz-aware display; the
    # isoformat with offset is the same instant.
    assert f"[watcher] one-shot -> fires at {when.astimezone(dt.UTC).isoformat()}" in out
