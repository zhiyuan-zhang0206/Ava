"""Tests for agent.startup._notify_desktop_permissions_at_startup.

Screen Recording and Accessibility keep separate status files from converge to
startup. This test module pins their shared one-notice delivery and per-file
atomic claim behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.startup import _notify_desktop_permissions_at_startup
from shared.accessibility import (
    AccessibilityState,
    AccessibilityStatus,
)
from shared.accessibility import (
    read_status as read_accessibility_status,
)
from shared.accessibility import (
    status_file_path as accessibility_status_file_path,
)
from shared.accessibility import (
    write_status as write_accessibility_status,
)
from shared.screen_capture import (
    ScreenCaptureState,
    ScreenCaptureStatus,
)
from shared.screen_capture import (
    read_status as read_screen_capture_status,
)
from shared.screen_capture import (
    status_file_path as screen_capture_status_file_path,
)
from shared.screen_capture import (
    write_status as write_screen_capture_status,
)

_SCREEN_FAULT = ScreenCaptureStatus(
    state=ScreenCaptureState.NO_GRANT,
    diagnostic="The permissions helper holds no Screen Recording grant. Fix: System Settings.",
)
_ACCESSIBILITY_FAULT = AccessibilityStatus(
    state=AccessibilityState.NOT_GRANTED,
    diagnostic="The permissions helper holds no Accessibility grant. Fix: System Settings.",
)


class _FakeUI:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def notify(self, **kwargs: object) -> int:
        self.calls.append(kwargs)
        return 1


@pytest.fixture
def fake_ui(monkeypatch: pytest.MonkeyPatch) -> _FakeUI:
    import ava as ava_module

    ui = _FakeUI()
    monkeypatch.setattr(ava_module, "ui", ui, raising=False)
    return ui


def _patch_status_homes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shared.screen_capture.ava_home", lambda: tmp_path)
    monkeypatch.setattr("shared.accessibility.ava_home", lambda: tmp_path)


class TestNotifyDesktopPermissionsAtStartup:
    @pytest.mark.asyncio
    async def test_noop_when_no_status_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_ui: _FakeUI
    ):
        _patch_status_homes(monkeypatch, tmp_path)

        await _notify_desktop_permissions_at_startup()

        assert fake_ui.calls == []

    @pytest.mark.asyncio
    async def test_screen_only_fault_keeps_the_existing_notice_shape(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_ui: _FakeUI
    ):
        _patch_status_homes(monkeypatch, tmp_path)
        write_screen_capture_status(_SCREEN_FAULT)

        await _notify_desktop_permissions_at_startup()

        assert fake_ui.calls == [
            {
                "title": _SCREEN_FAULT.headline,
                "content": _SCREEN_FAULT.diagnostic,
                "priority": "P1",
            }
        ]
        assert read_screen_capture_status() is None
        assert read_accessibility_status() is None

    @pytest.mark.asyncio
    async def test_accessibility_only_fault_notifies_and_clears(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_ui: _FakeUI
    ):
        _patch_status_homes(monkeypatch, tmp_path)
        write_accessibility_status(_ACCESSIBILITY_FAULT)

        await _notify_desktop_permissions_at_startup()

        assert fake_ui.calls == [
            {
                "title": _ACCESSIBILITY_FAULT.headline,
                "content": _ACCESSIBILITY_FAULT.diagnostic,
                "priority": "P1",
            }
        ]
        assert read_accessibility_status() is None

    @pytest.mark.asyncio
    async def test_both_faults_post_one_combined_notice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_ui: _FakeUI
    ):
        _patch_status_homes(monkeypatch, tmp_path)
        write_screen_capture_status(_SCREEN_FAULT)
        write_accessibility_status(_ACCESSIBILITY_FAULT)

        await _notify_desktop_permissions_at_startup()

        assert fake_ui.calls == [
            {
                "title": "Desktop permissions missing",
                "content": (
                    f"{_SCREEN_FAULT.headline}: {_SCREEN_FAULT.diagnostic}\n\n"
                    f"{_ACCESSIBILITY_FAULT.headline}: {_ACCESSIBILITY_FAULT.diagnostic}"
                ),
                "priority": "P1",
            }
        ]
        assert read_screen_capture_status() is None
        assert read_accessibility_status() is None

    @pytest.mark.asyncio
    async def test_notify_failure_restores_both_claims(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _patch_status_homes(monkeypatch, tmp_path)
        write_screen_capture_status(_SCREEN_FAULT)
        write_accessibility_status(_ACCESSIBILITY_FAULT)

        class FailingUI:
            def notify(self, **kwargs: object) -> int:
                raise RuntimeError("gateway unreachable")

        import ava as ava_module

        monkeypatch.setattr(ava_module, "ui", FailingUI(), raising=False)

        await _notify_desktop_permissions_at_startup()

        assert read_screen_capture_status() == _SCREEN_FAULT
        assert read_accessibility_status() == _ACCESSIBILITY_FAULT
        assert not screen_capture_status_file_path().with_suffix(".processing").exists()
        assert not accessibility_status_file_path().with_suffix(".processing").exists()

    @pytest.mark.asyncio
    async def test_concurrent_claim_loser_skips_only_that_status_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_ui: _FakeUI
    ):
        _patch_status_homes(monkeypatch, tmp_path)
        write_screen_capture_status(_SCREEN_FAULT)
        write_accessibility_status(_ACCESSIBILITY_FAULT)
        screen_capture_status_file_path().rename(
            screen_capture_status_file_path().with_suffix(".processing")
        )

        await _notify_desktop_permissions_at_startup()

        assert fake_ui.calls == [
            {
                "title": _ACCESSIBILITY_FAULT.headline,
                "content": _ACCESSIBILITY_FAULT.diagnostic,
                "priority": "P1",
            }
        ]
        assert screen_capture_status_file_path().with_suffix(".processing").exists()
        assert read_accessibility_status() is None

    @pytest.mark.asyncio
    async def test_available_statuses_are_cleaned_without_notifying(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_ui: _FakeUI
    ):
        _patch_status_homes(monkeypatch, tmp_path)
        write_screen_capture_status(ScreenCaptureStatus(state=ScreenCaptureState.AVAILABLE))
        write_accessibility_status(AccessibilityStatus(state=AccessibilityState.GRANTED))

        await _notify_desktop_permissions_at_startup()

        assert fake_ui.calls == []
        assert read_screen_capture_status() is None
        assert read_accessibility_status() is None
