"""Cluster-timezone resolution + process-wide application tests.

Covers the one-cluster-clock boot hook (Task #1758, user ruling 2026-08-27):
a process holding an authoritative AVA_TIMEZONE applies it as its own TZ on
POSIX; a process without one (settings-lite / bare checkout) is untouched.
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from zoneinfo import ZoneInfo

import pytest

from shared.config import apply_cluster_timezone, cluster_tz, settings
from shared.config.general import GeneralSettings


@pytest.fixture(autouse=True)
def _restore_process_tz() -> Generator[None, None, None]:
    """Restore the process's original TZ env + tzset state after each test.

    apply_cluster_timezone mutates the process wall clock (os.environ["TZ"] +
    time.tzset()); the suite must not leak that into other tests. pytest's
    monkeypatch restores the env var itself; this fixture re-tzset()s from the
    restored env so the C library agrees with the env again.
    """
    yield
    if hasattr(time, "tzset"):
        time.tzset()


def _set_cluster_tz(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``settings.general.timezone`` authoritative for this test.

    ``cluster_tz()`` treats the field as authoritative only when it was
    explicitly set at Settings build (``model_fields_set``), and pydantic's
    ``model_fields_set`` is a read-only property — so the test swaps in a
    freshly constructed ``GeneralSettings`` whose fields-set carries the
    timezone. ``model_construct`` skips env reads and validation, which is
    exactly the isolated shape a unit test wants. This is the test-side
    equivalent of a process that booted with AVA_TIMEZONE in its env / .env /
    bootstrap.
    """
    monkeypatch.setattr(settings, "general", GeneralSettings.model_construct(timezone=name))


def _clear_cluster_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``settings.general.timezone`` non-authoritative (settings-lite)."""
    monkeypatch.setattr(settings, "general", GeneralSettings.model_construct())


def test_cluster_tz_returns_zoneinfo_when_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_cluster_tz(monkeypatch, "Asia/Shanghai")
    assert cluster_tz() == ZoneInfo("Asia/Shanghai")


def test_cluster_tz_none_without_authoritative_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cluster_tz(monkeypatch)
    assert cluster_tz() is None


def test_cluster_tz_none_for_invalid_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: Settings already fails fast on a bad IANA name at
    construction, but a display path must degrade to the host zone rather
    than crash if one ever slips through."""
    _set_cluster_tz(monkeypatch, "Not/AZone")
    assert cluster_tz() is None


def test_apply_sets_tz_and_rezones_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """With an authoritative AVA_TIMEZONE, the process wall clock follows the
    cluster zone: os.environ["TZ"] is set and time.localtime() re-resolves to
    it (tm_gmtoff is the POSIX-local observable; skip where unavailable)."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only")
    _set_cluster_tz(monkeypatch, "Asia/Shanghai")
    monkeypatch.delenv("TZ", raising=False)
    apply_cluster_timezone()
    assert os.environ["TZ"] == "Asia/Shanghai"
    gmtoff = getattr(time.localtime(), "tm_gmtoff", None)
    if gmtoff is not None:
        assert gmtoff == 8 * 3600


def test_apply_noop_without_authoritative_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process without an authoritative value is left untouched — forcing
    the field default (America/Los_Angeles) onto a settings-lite maintenance
    verb or a bare checkout would be wrong."""
    _clear_cluster_tz(monkeypatch)
    monkeypatch.delenv("TZ", raising=False)
    apply_cluster_timezone()
    assert "TZ" not in os.environ


def test_apply_sets_env_even_without_tzset(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows there is no time.tzset: the hook still exports TZ for
    subprocess children and must not raise. The tzset indirection (_tzset) is
    patched — never the time module itself — so the process wall clock stays
    untouched and no TZ state leaks into later tests."""
    monkeypatch.setattr("shared.config._tzset", lambda: None)
    _set_cluster_tz(monkeypatch, "Asia/Shanghai")
    apply_cluster_timezone()
    assert os.environ["TZ"] == "Asia/Shanghai"
