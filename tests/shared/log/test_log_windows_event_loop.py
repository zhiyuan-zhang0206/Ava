"""shared.log._configure_windows_event_loop_policy — Windows boot policy tests.

The Windows ProactorEventLoop rejects psycopg async sockets; the boot seams
must install the Selector policy exactly on Windows. Non-Windows must stay a
no-op (the policy class does not exist there).
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from shared import log


class _FakePolicy:
    """Stand-in for asyncio.WindowsSelectorEventLoopPolicy (Win-only)."""


def test_win32_installs_selector_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(asyncio, "WindowsSelectorEventLoopPolicy", _FakePolicy, raising=False)
    monkeypatch.setattr(asyncio, "set_event_loop_policy", seen.append)

    log._configure_windows_event_loop_policy()

    assert len(seen) == 1
    assert isinstance(seen[0], _FakePolicy)


def test_non_windows_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(asyncio, "set_event_loop_policy", seen.append)

    log._configure_windows_event_loop_policy()

    assert not seen


def test_win32_without_policy_class_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(asyncio, "WindowsSelectorEventLoopPolicy", raising=False)
    monkeypatch.setattr(asyncio, "set_event_loop_policy", seen.append)

    log._configure_windows_event_loop_policy()

    assert not seen
