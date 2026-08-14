"""Tests for agent.startup._notify_screen_capture_at_startup.

The screen-capture status-file mechanism lives in shared.screen_capture (see
tests/shared/test_screen_capture.py); the agent-startup notification that reads
the file and calls ava.ui.notify lives in the agent layer, tested here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.startup import _notify_screen_capture_at_startup
from shared.screen_capture import (
    ScreenCaptureState,
    ScreenCaptureStatus,
    read_status,
    status_file_path,
    write_status,
)

_NO_GRANT = ScreenCaptureStatus(
    state=ScreenCaptureState.NO_GRANT,
    diagnostic="The permissions helper holds no Screen Recording grant. Fix: System Settings.",
)
_UNREACHABLE = ScreenCaptureStatus(
    state=ScreenCaptureState.HELPER_UNREACHABLE,
    diagnostic="The permissions helper did not answer on its socket. Check its launchd job.",
)


class _FakeUI:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def notify(self, **kwargs):
        self.calls.append(kwargs)  # pyright: ignore[reportUnknownMemberType]
        return 1


@pytest.fixture
def fake_ui(monkeypatch: pytest.MonkeyPatch):
    import ava as ava_module

    ui = _FakeUI()
    monkeypatch.setattr(ava_module, "ui", ui, raising=False)
    return ui


class TestNotifyScreenCaptureAtStartup:
    @pytest.mark.asyncio
    async def test_noop_when_no_status_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr("shared.screen_capture.ava_home", lambda: tmp_path)
        # Should not raise — just return silently
        await _notify_screen_capture_at_startup()

    @pytest.mark.asyncio
    async def test_noop_when_status_is_available(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setattr("shared.screen_capture.ava_home", lambda: tmp_path)
        write_status(ScreenCaptureStatus(state=ScreenCaptureState.AVAILABLE))

        await _notify_screen_capture_at_startup()
        # File should be cleared even when available (cleanup)
        assert read_status() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [_NO_GRANT, _UNREACHABLE], ids=["no_grant", "unreachable"])
    async def test_notifies_with_the_measured_fault_and_clears(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_ui, status
    ):
        """Both faults notify, each carrying its own headline and fix -- the
        banner renders what the probe measured rather than a fixed story."""
        monkeypatch.setattr("shared.screen_capture.ava_home", lambda: tmp_path)
        write_status(status)  # pyright: ignore[reportUnknownArgumentType]

        await _notify_screen_capture_at_startup()

        assert len(fake_ui.calls) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        assert fake_ui.calls[0]["title"] == status.headline  # pyright: ignore[reportUnknownMemberType]
        assert fake_ui.calls[0]["content"] == status.diagnostic  # pyright: ignore[reportUnknownMemberType]
        assert fake_ui.calls[0]["priority"] == "P1"  # pyright: ignore[reportUnknownMemberType]
        assert read_status() is None

    @pytest.mark.asyncio
    async def test_the_two_faults_do_not_render_alike(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_ui
    ):
        monkeypatch.setattr("shared.screen_capture.ava_home", lambda: tmp_path)
        for status in (_NO_GRANT, _UNREACHABLE):
            write_status(status)
            await _notify_screen_capture_at_startup()

        assert fake_ui.calls[0]["title"] != fake_ui.calls[1]["title"]  # pyright: ignore[reportUnknownMemberType]
        assert fake_ui.calls[0]["content"] != fake_ui.calls[1]["content"]  # pyright: ignore[reportUnknownMemberType]

    @pytest.mark.asyncio
    async def test_does_not_clear_on_notify_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """A failed notify (e.g. gateway unreachable) leaves the status file in
        place so the next agent startup retries instead of dropping the notice."""
        monkeypatch.setattr("shared.screen_capture.ava_home", lambda: tmp_path)
        write_status(_NO_GRANT)

        class FailingUI:
            def notify(self, **kwargs):
                raise RuntimeError("gateway unreachable")

        import ava as ava_module

        monkeypatch.setattr(ava_module, "ui", FailingUI(), raising=False)

        await _notify_screen_capture_at_startup()

        # Status file is restored for retry; no .processing claim is left behind.
        restored = read_status()
        assert restored is not None
        assert restored.state is ScreenCaptureState.NO_GRANT
        assert not status_file_path().with_suffix(".processing").exists()

    @pytest.mark.asyncio
    async def test_loser_of_atomic_claim_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_ui
    ):
        """When another concurrently-starting agent already claimed the status
        file (atomic rename), this one finds nothing and fires no duplicate."""
        monkeypatch.setattr("shared.screen_capture.ava_home", lambda: tmp_path)
        write_status(_NO_GRANT)
        # Simulate the winning starter having already claimed the file.
        status_file_path().rename(status_file_path().with_suffix(".processing"))

        await _notify_screen_capture_at_startup()
        assert fake_ui.calls == []  # pyright: ignore[reportUnknownMemberType]
