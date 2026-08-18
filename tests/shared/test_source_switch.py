"""`shared.source_switch` — the update's source-switch window marker.

The marker is what the healthcheck respawn path consults before launching
during an update: `respawn_service` holds back while the marker is fresh, so a
spawn can never read a half-written tree. These tests pin the marker's own
contract — mark / clear / TTL expiry / fail-open reads — with the marker path
redirected to a temp dir so the suite never touches a real cluster home.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import shared.source_switch as ss


@pytest.fixture
def marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The marker file, with the module's path redirected to tmp_path."""
    path = tmp_path / "run" / ss._MARKER_NAME
    monkeypatch.setattr(ss, "_marker_path", lambda: path)
    return path


def test_missing_marker_reads_as_not_switching(marker: Path) -> None:
    assert ss.is_switching() is False


def test_mark_then_clear(marker: Path) -> None:
    ss.mark_switching()
    assert marker.exists()
    assert ss.is_switching() is True
    ss.clear_switching()
    assert not marker.exists()
    assert ss.is_switching() is False


def test_expired_marker_reads_as_not_switching(
    marker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed update leaves the marker behind; once its TTL passes it must
    fail open — a stuck marker holding respawns back forever would be worse
    than the torn-read it guards against."""
    marker.parent.mkdir(parents=True)
    marker.write_text(str(time.time() - ss._SWITCH_TTL_S - 1) + "\n")
    assert ss.is_switching() is False


def test_fresh_marker_respects_clock(marker: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    monkeypatch.setattr(ss.time, "time", lambda: now)
    ss.mark_switching()
    assert ss.is_switching() is True
    monkeypatch.setattr(ss.time, "time", lambda: now + ss._SWITCH_TTL_S / 2)
    assert ss.is_switching() is True
    monkeypatch.setattr(ss.time, "time", lambda: now + ss._SWITCH_TTL_S + 1)
    assert ss.is_switching() is False


def test_garbage_marker_reads_as_not_switching(marker: Path) -> None:
    marker.parent.mkdir(parents=True)
    marker.write_text("not-a-timestamp\n")
    assert ss.is_switching() is False


def test_mark_creates_parent_dirs(marker: Path) -> None:
    """The marker lands under the run dir, which may not exist yet on a fresh
    home — mark_switching creates it rather than failing."""
    ss.mark_switching()
    assert marker.exists()
    assert ss.is_switching() is True


def test_unwritable_marker_does_not_raise(marker: Path, tmp_path: Path) -> None:
    """Best-effort by design: an unwritable run dir must not abort an update.
    A file standing where the marker's parent dir should be makes every write
    fail; mark/clear must swallow the error."""
    blocker = tmp_path / "run"
    blocker.write_text("in the way")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ss, "_marker_path", lambda: blocker / ss._MARKER_NAME)
    try:
        ss.mark_switching()  # does not raise
        ss.clear_switching()  # does not raise
        assert ss.is_switching() is False
    finally:
        monkeypatch.undo()
