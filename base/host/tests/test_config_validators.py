"""Unit tests for base.host.config_validators.

The browser gate judges the `Host` it is given: Chrome and npx come from the programs on
its PATH. The display comes from the process environment, so the cases that need one use a
macOS host (it always has a display) and the cases that need none run only in a process that
has none (headless CI, a Mac); a Linux desktop skips them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from base.host.config_validators import ValidationResult, read_time_capability, validate
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, LINUX_X86, fake_host

_CHROME = __file__  # an existing path stands in for the Chrome binary
_NPX = "/usr/bin/npx"

needs_no_display = pytest.mark.skipif(
    bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="the process has a display",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mac(*, npx: bool) -> Host:
    """A macOS host (always a display) with or without npx on its PATH."""
    return fake_host(platform=DARWIN_ARM64, programs={"npx": _NPX} if npx else {})


def _linux(*, chrome: bool, npx: bool) -> Host:
    """A Linux host with Chrome and npx as given."""
    programs: dict[str, str] = {}
    if chrome:
        programs["google-chrome"] = _CHROME
    if npx:
        programs["npx"] = _NPX
    return fake_host(platform=LINUX_X86, programs=programs)


# ---------------------------------------------------------------------------
# browser_enabled
# ---------------------------------------------------------------------------


def test_browser_enabled_false_always_ok():
    """False is always ok regardless of browser capability."""
    result = validate(
        "browser_enabled", False, chrome_binary=None, host=_linux(chrome=False, npx=False)
    )
    assert result.ok
    assert result.reason is None


def test_browser_enabled_true_when_capable_ok():
    result = validate("browser_enabled", True, chrome_binary=_CHROME, host=_mac(npx=True))
    assert result.ok
    assert result.reason is None


@needs_no_display
def test_browser_enabled_true_without_display_not_ok():
    host = _linux(chrome=True, npx=True)
    result = validate("browser_enabled", True, chrome_binary=None, host=host)
    assert not result.ok
    assert result.reason is not None
    assert "display" in result.reason.lower()


def test_browser_enabled_true_with_display_but_no_chrome_not_ok(tmp_path: Path):
    """A configured Chrome path that does not exist is the reason, with npx missing too."""
    missing = str(tmp_path / "no-chrome")
    result = validate("browser_enabled", True, chrome_binary=missing, host=_mac(npx=False))
    assert not result.ok
    assert result.reason is not None
    assert "chrome" in result.reason.lower()


def test_browser_enabled_true_with_display_and_chrome_but_no_npx_not_ok():
    """Display + Chrome are fine -> npx is the reason."""
    result = validate("browser_enabled", True, chrome_binary=_CHROME, host=_mac(npx=False))
    assert not result.ok
    assert result.reason is not None
    assert "npx" in result.reason.lower()


@needs_no_display
def test_browser_enabled_fail_safe_uncertain_display():
    """When the display probe would return False (uncertain), True is rejected."""
    result = validate(
        "browser_enabled", True, chrome_binary=None, host=_linux(chrome=True, npx=True)
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# chrome_binary
# ---------------------------------------------------------------------------


def test_chrome_binary_existing_path_ok(tmp_path: Path):
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh")
    result = validate("chrome_binary", str(fake), chrome_binary=None, host=fake_host())
    assert result.ok
    assert result.reason is None


def test_chrome_binary_missing_path_not_ok(tmp_path: Path):
    missing = tmp_path / "no_chrome_here"
    result = validate("chrome_binary", str(missing), chrome_binary=None, host=fake_host())
    assert not result.ok
    assert result.reason is not None
    assert "not found" in result.reason.lower()


# ---------------------------------------------------------------------------
# ops_concurrency
# ---------------------------------------------------------------------------


def test_ops_concurrency_boundary_1_ok():
    assert validate("ops_concurrency", 1, chrome_binary=None, host=fake_host()).ok


def test_ops_concurrency_boundary_64_ok():
    assert validate("ops_concurrency", 64, chrome_binary=None, host=fake_host()).ok


def test_ops_concurrency_zero_not_ok():
    result = validate("ops_concurrency", 0, chrome_binary=None, host=fake_host())
    assert not result.ok
    assert result.reason is not None
    assert "1" in result.reason and "64" in result.reason


def test_ops_concurrency_65_not_ok():
    result = validate("ops_concurrency", 65, chrome_binary=None, host=fake_host())
    assert not result.ok


def test_ops_concurrency_bool_true_not_ok():
    """True == 1 as int, but bools are rejected explicitly."""
    result = validate("ops_concurrency", True, chrome_binary=None, host=fake_host())
    assert not result.ok


def test_ops_concurrency_bool_false_not_ok():
    """False == 0 as int, but bools are rejected explicitly."""
    result = validate("ops_concurrency", False, chrome_binary=None, host=fake_host())
    assert not result.ok


def test_ops_concurrency_string_not_ok():
    result = validate("ops_concurrency", "3", chrome_binary=None, host=fake_host())
    assert not result.ok


def test_ops_concurrency_float_not_ok():
    result = validate("ops_concurrency", 3.5, chrome_binary=None, host=fake_host())
    assert not result.ok


# ---------------------------------------------------------------------------
# unregistered field (no-precondition case)
# ---------------------------------------------------------------------------


def test_machine_description_unregistered_always_ok():
    """machine_description has no validator entry; any value is accepted."""
    result = validate("machine_description", "anything", chrome_binary=None, host=fake_host())
    assert result.ok
    assert result.reason is None


def test_unknown_field_always_ok():
    result = validate("nonexistent_field_xyz", 42, chrome_binary=None, host=fake_host())
    assert result.ok


# ---------------------------------------------------------------------------
# read_time_capability
# ---------------------------------------------------------------------------


def test_read_time_capability_browser_enabled_returns_validation_result():
    result = read_time_capability("browser_enabled", chrome_binary=_CHROME, host=_mac(npx=True))
    assert isinstance(result, ValidationResult)
    assert result.ok


@needs_no_display
def test_read_time_capability_browser_enabled_not_ok_without_display():
    host = _linux(chrome=False, npx=False)
    result = read_time_capability("browser_enabled", chrome_binary=None, host=host)
    assert result is not None
    assert not result.ok


def test_read_time_capability_other_host_fields_return_none():
    """All other host fields have no static gate; validity depends on the value."""
    for field in (
        "chrome_binary",
        "ops_concurrency",
        "machine_description",
        "some_unknown_field",
    ):
        assert read_time_capability(field, chrome_binary=None, host=fake_host()) is None, (
            f"expected None for {field!r}"
        )
