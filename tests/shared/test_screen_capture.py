"""Tests for shared.screen_capture -- the status type and its status file.

The probe that produces a status lives with the process it interrogates
(`services.permissions_helper.client.check_screen_capture`, covered by
tests/services/test_permissions_helper.py); what is pinned here is the three-state
result and the file that carries it from converge to agent startup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.screen_capture import (
    ScreenCaptureState,
    ScreenCaptureStatus,
    clear_status,
    read_status,
    status_file_path,
    write_status,
)


class TestScreenCaptureStatus:
    def test_only_the_available_state_reads_as_available(self):
        """An unread grant is not a granted one -- both faults stay unavailable."""
        assert ScreenCaptureStatus(state=ScreenCaptureState.AVAILABLE).available is True
        assert ScreenCaptureStatus(state=ScreenCaptureState.NO_GRANT).available is False
        assert ScreenCaptureStatus(state=ScreenCaptureState.HELPER_UNREACHABLE).available is False

    def test_headline_distinguishes_the_two_faults(self):
        """The notification title alone must say which of the two problems it is."""
        no_grant = ScreenCaptureStatus(state=ScreenCaptureState.NO_GRANT).headline
        unreachable = ScreenCaptureStatus(state=ScreenCaptureState.HELPER_UNREACHABLE).headline
        assert no_grant != unreachable
        assert "permission" in no_grant.lower()
        assert "helper" in unreachable.lower()

    def test_roundtrip_json(self):
        status = ScreenCaptureStatus(
            state=ScreenCaptureState.NO_GRANT, diagnostic="test diagnostic"
        )
        assert json.loads(status.to_json()) == {
            "state": "no_grant",
            "diagnostic": "test diagnostic",
        }
        assert ScreenCaptureStatus.from_json(status.to_json()) == status

    def test_from_file_returns_none_when_absent(self, tmp_path: Path):
        assert ScreenCaptureStatus.from_file(tmp_path / "nonexistent.json") is None

    def test_from_file_returns_none_on_corrupt(self, tmp_path: Path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json")
        assert ScreenCaptureStatus.from_file(path) is None

    def test_from_file_returns_none_on_unknown_state(self, tmp_path: Path):
        path = tmp_path / "unknown.json"
        path.write_text(json.dumps({"state": "maybe", "diagnostic": ""}))
        assert ScreenCaptureStatus.from_file(path) is None

    def test_from_file_returns_none_on_a_file_written_by_an_older_build(self, tmp_path: Path):
        """The pre-state shape carried a bare bool; an unreadable file is rewritten
        by the next converge, so the changeover costs at most one skipped notice."""
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps({"available": False, "diagnostic": "started from SSH"}))
        assert ScreenCaptureStatus.from_file(path) is None


class TestStatusFile:
    def test_write_read_clear_cycle(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr("shared.screen_capture.ava_home", lambda: tmp_path)
        write_status(ScreenCaptureStatus(state=ScreenCaptureState.NO_GRANT, diagnostic="test"))

        assert status_file_path().exists()
        read = read_status()
        assert read is not None
        assert read.state is ScreenCaptureState.NO_GRANT
        assert read.diagnostic == "test"

        clear_status()
        assert not status_file_path().exists()
        assert read_status() is None
