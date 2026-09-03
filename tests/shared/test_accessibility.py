"""Tests for shared.accessibility -- the status type and its status file.

The probe that produces a status lives with the permissions helper client; this
module owns the three-state result and the file that carries it from converge to
agent startup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.accessibility import (
    AccessibilityState,
    AccessibilityStatus,
    clear_status,
    read_status,
    status_file_path,
    write_status,
)


class TestAccessibilityStatus:
    def test_only_granted_is_available(self):
        assert AccessibilityStatus(state=AccessibilityState.GRANTED).available is True
        assert AccessibilityStatus(state=AccessibilityState.NOT_GRANTED).available is False
        assert AccessibilityStatus(state=AccessibilityState.HELPER_UNREACHABLE).available is False

    def test_headlines_distinguish_the_two_faults(self):
        granted = AccessibilityStatus(state=AccessibilityState.GRANTED).headline
        not_granted = AccessibilityStatus(state=AccessibilityState.NOT_GRANTED).headline
        unreachable = AccessibilityStatus(state=AccessibilityState.HELPER_UNREACHABLE).headline
        assert granted == "Accessibility available"
        assert not_granted == "Accessibility permission missing"
        assert unreachable == "Permissions helper unreachable"

    def test_roundtrip_json(self):
        status = AccessibilityStatus(
            state=AccessibilityState.NOT_GRANTED, diagnostic="test diagnostic"
        )
        assert json.loads(status.to_json()) == {
            "state": "not_granted",
            "diagnostic": "test diagnostic",
        }
        assert AccessibilityStatus.from_json(status.to_json()) == status

    def test_from_file_returns_none_when_absent(self, tmp_path: Path):
        assert AccessibilityStatus.from_file(tmp_path / "nonexistent.json") is None

    def test_from_file_returns_none_on_corrupt(self, tmp_path: Path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json")
        assert AccessibilityStatus.from_file(path) is None

    def test_from_file_returns_none_on_unknown_state(self, tmp_path: Path):
        path = tmp_path / "unknown.json"
        path.write_text(json.dumps({"state": "maybe", "diagnostic": ""}))
        assert AccessibilityStatus.from_file(path) is None

    def test_from_file_returns_none_on_a_legacy_shape(self, tmp_path: Path):
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps({"available": False, "diagnostic": "started from SSH"}))
        assert AccessibilityStatus.from_file(path) is None


class TestStatusFile:
    def test_write_read_clear_cycle(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr("shared.accessibility.ava_home", lambda: tmp_path)
        write_status(AccessibilityStatus(state=AccessibilityState.NOT_GRANTED, diagnostic="test"))

        assert status_file_path().exists()
        status = read_status()
        assert status is not None
        assert status.state is AccessibilityState.NOT_GRANTED
        assert status.diagnostic == "test"

        clear_status()
        assert not status_file_path().exists()
        assert read_status() is None
