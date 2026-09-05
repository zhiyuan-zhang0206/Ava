"""shared/log — file-sink rotation predicate (Task #434).

`_rotate_by_size_or_day` is the JSONL sink's rotation predicate: 100 MB
ceiling OR the message's date moved past the file's creation date. The
time half is anchored on the file's own ctime so the two processes sharing
`agent-{N}.log` (kernel + exec) evaluate the same decision.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.log import _FILE_SINK_SIZE_LIMIT, _rotate_by_size_or_day


def _message(t: datetime.datetime) -> SimpleNamespace:
    return SimpleNamespace(record={"time": t})


def test_no_rotation_under_limit_same_day(tmp_path: Path) -> None:
    f = tmp_path / "agent-1.log"
    f.write_text("x" * 1000)
    with f.open("a") as fh:
        assert not _rotate_by_size_or_day(_message(datetime.datetime.now(datetime.UTC)), fh)


def test_rotates_when_size_past_ceiling(tmp_path: Path) -> None:
    f = tmp_path / "agent-1.log"
    f.write_text("x" * (_FILE_SINK_SIZE_LIMIT + 1))
    with f.open("a") as fh:
        assert _rotate_by_size_or_day(_message(datetime.datetime.now(datetime.UTC)), fh)


def test_rotates_when_message_date_past_file_creation(tmp_path: Path) -> None:
    f = tmp_path / "agent-1.log"
    f.write_text("x")  # created now
    tomorrow = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    with f.open("a") as fh:
        assert _rotate_by_size_or_day(_message(tomorrow), fh)


def test_creation_date_is_re_read_after_rotation(tmp_path: Path) -> None:
    """After a rotation the sink holds the fresh base path, whose ctime is
    today — a same-day message must NOT rotate again."""
    f = tmp_path / "agent-1.log"
    f.write_text("x")
    with f.open("a") as fh:
        assert _rotate_by_size_or_day(_message(datetime.datetime.now(datetime.UTC)), fh) is False


def test_day_boundary_is_utc_not_message_local_tz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """loguru stamps a message's tzinfo from the machine's local zone, so the
    day boundary drifted per host when it followed the message's own tz (tz
    audit, 2026-08, PR-6) — it must follow UTC everywhere instead. Pins the
    file's ctime to a fixed instant (rather than relying on the real wall
    clock) so the scenario is deterministic regardless of what hour the test
    happens to run."""
    f = tmp_path / "agent-1.log"
    f.write_text("x")
    # ctime: 2026-06-15 23:50 UTC — "today" is June 15 in UTC. Only this
    # test's own file is faked — Path.stat is used throughout pytest's own
    # machinery, so the fake must fall through to the real implementation for
    # every other path.
    fixed_ctime = datetime.datetime(2026, 6, 15, 23, 50, tzinfo=datetime.UTC).timestamp()
    real_stat = Path.stat

    def fake_stat(self):
        if self == f:
            return SimpleNamespace(st_ctime=fixed_ctime)
        return real_stat(self)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(Path, "stat", fake_stat)  # pyright: ignore[reportUnknownArgumentType]

    # The message instant is 20 minutes later — 2026-06-16 00:10 UTC, genuinely
    # the NEXT UTC calendar day. loguru stamps the message in the writing
    # machine's local tz; here that machine is UTC-1, whose wall clock still
    # reads "2026-06-15 23:10" — the OLD day, even though the day already
    # rolled over in UTC.
    message_instant_utc = datetime.datetime(2026, 6, 16, 0, 10, tzinfo=datetime.UTC)
    local_view = message_instant_utc.astimezone(datetime.timezone(datetime.timedelta(hours=-1)))
    assert (
        local_view.date() < message_instant_utc.date()
    )  # sanity: local wall clock hasn't rolled over yet

    with f.open("a") as fh:
        # The real (UTC) day already changed — must rotate regardless of what
        # calendar date the message's own local tz happens to read.
        assert _rotate_by_size_or_day(_message(local_view), fh) is True
