"""Session identities distinguish transcript prefixes before child output."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shared import session_log


@pytest.mark.parametrize("pid", [None, 42])
def test_header_format(pid: int | None) -> None:
    name = "session-\N{LATIN SMALL LETTER E WITH ACUTE}"
    before = datetime.now(UTC)
    header = session_log.session_log_header(name, pid=pid)
    after = datetime.now(UTC)
    prefix = f"--- ava session {name} start="
    suffix = f" pid={pid} ---\n" if pid is not None else " ---\n"
    line = header.decode("utf-8")
    assert line.startswith(prefix)
    assert line.endswith(suffix)
    timestamp = datetime.fromisoformat(line[len(prefix) : -len(suffix)])
    assert before <= timestamp <= after
    assert timestamp.utcoffset() == UTC.utcoffset(timestamp)
    assert line.count("\n") == 1
    assert (" pid=" in line) is (pid is not None)


def test_header_identity_changes_with_name_pid_and_time(monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock:
        instant = datetime(2026, 9, 9, microsecond=123456, tzinfo=UTC)

        @classmethod
        def now(cls, tz: object) -> datetime:
            assert tz is UTC
            return cls.instant

    monkeypatch.setattr(session_log, "datetime", Clock)
    first = session_log.session_log_header("one", pid=1)
    other_name = session_log.session_log_header("two", pid=1)
    other_pid = session_log.session_log_header("one", pid=2)
    Clock.instant = Clock.instant.replace(microsecond=123457)
    later = session_log.session_log_header("one", pid=1)
    assert len({first, other_name, other_pid, later}) == 4
    assert b".123456+00:00" in first


def test_new_log_has_header_before_output(tmp_path: Path) -> None:
    path = tmp_path / "session.log"
    fd, created = session_log.open_session_log(path, "one", pid=42)
    try:
        assert created
        header = path.read_bytes()
        assert header.startswith(b"--- ava session one start=")
        assert header.endswith(b" pid=42 ---\n")
        os.write(fd, b"child output\n")
        assert path.read_bytes() == header + b"child output\n"
    finally:
        os.close(fd)


@pytest.mark.parametrize("content", [b"", b"legacy banner\n"])
def test_existing_log_is_unchanged_and_appends(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "session.log"
    path.write_bytes(content)
    fd, created = session_log.open_session_log(path, "one", pid=42)
    try:
        assert not created
        assert path.read_bytes() == content
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"next output\n")
        assert path.read_bytes() == content + b"next output\n"
    finally:
        os.close(fd)


def test_reopening_new_log_does_not_repeat_header(tmp_path: Path) -> None:
    path = tmp_path / "session.log"
    fd, _ = session_log.open_session_log(path, "one")
    os.close(fd)
    original = path.read_bytes()
    fd, created = session_log.open_session_log(path, "two", pid=42)
    os.close(fd)
    assert not created
    assert path.read_bytes() == original
