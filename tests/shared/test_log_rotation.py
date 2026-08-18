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
