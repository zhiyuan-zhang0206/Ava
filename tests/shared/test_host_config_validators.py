"""Unit tests for shared.host_config_validators.

The aggregate browser_capable() gate and the individual display / Chrome probes
are monkeypatched so the tests are deterministic on any host (headless CI has
no display / Chrome / npx; dev machines have all three).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import shared.host_config_validators as hcv
from shared.host_config_validators import ValidationResult, read_time_capability, validate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_browser_capable(monkeypatch: pytest.MonkeyPatch, *, capable: bool) -> None:
    # browser_capable is the aggregate gate (display + Chrome + npx) imported
    # into hcv; the validator consults it first. It probes inside
    # shared.platform_probes, so patching hcv.display_available alone cannot
    # influence it — patch the bound name in hcv directly.
    monkeypatch.setattr(hcv, "browser_capable", lambda: capable)


def _patch_display(monkeypatch: pytest.MonkeyPatch, *, available: bool) -> None:
    # display_available is imported into hcv from shared.platform_probes; patch
    # the name as bound in the validator module (where the validator calls it).
    monkeypatch.setattr(hcv, "display_available", lambda: available)


def _patch_chrome(monkeypatch: pytest.MonkeyPatch, *, available: bool) -> None:
    # The validator resolves Chrome via resolve_chrome_binary (imported into
    # hcv) and then exists()-checks it. Returning a path that exists -> usable;
    # None -> unusable. Patch the bound name in the validator module.
    if available:
        monkeypatch.setattr(hcv, "resolve_chrome_binary", lambda: __file__)  # an existing path
    else:
        monkeypatch.setattr(hcv, "resolve_chrome_binary", lambda: None)


# ---------------------------------------------------------------------------
# browser_enabled
# ---------------------------------------------------------------------------


def test_browser_enabled_false_always_ok(monkeypatch: pytest.MonkeyPatch):
    """False is always ok regardless of browser capability."""
    _patch_browser_capable(monkeypatch, capable=False)
    result = validate("browser_enabled", False)
    assert result.ok
    assert result.reason is None


def test_browser_enabled_true_when_capable_ok(monkeypatch: pytest.MonkeyPatch):
    _patch_browser_capable(monkeypatch, capable=True)
    result = validate("browser_enabled", True)
    assert result.ok
    assert result.reason is None


def test_browser_enabled_true_without_display_not_ok(monkeypatch: pytest.MonkeyPatch):
    _patch_browser_capable(monkeypatch, capable=False)
    _patch_display(monkeypatch, available=False)
    _patch_chrome(monkeypatch, available=True)
    result = validate("browser_enabled", True)
    assert not result.ok
    assert result.reason is not None
    assert "display" in result.reason.lower()


def test_browser_enabled_true_with_display_but_no_chrome_not_ok(monkeypatch: pytest.MonkeyPatch):
    _patch_browser_capable(monkeypatch, capable=False)
    _patch_display(monkeypatch, available=True)
    _patch_chrome(monkeypatch, available=False)
    result = validate("browser_enabled", True)
    assert not result.ok
    assert result.reason is not None
    assert "chrome" in result.reason.lower()


def test_browser_enabled_true_with_display_and_chrome_but_no_npx_not_ok(
    monkeypatch: pytest.MonkeyPatch,
):
    """Capability gate failed but display + Chrome are fine -> npx is the reason."""
    _patch_browser_capable(monkeypatch, capable=False)
    _patch_display(monkeypatch, available=True)
    _patch_chrome(monkeypatch, available=True)
    result = validate("browser_enabled", True)
    assert not result.ok
    assert result.reason is not None
    assert "npx" in result.reason.lower()


def test_browser_enabled_fail_safe_uncertain_display(monkeypatch: pytest.MonkeyPatch):
    """When display probe would return False (uncertain), True is rejected."""
    _patch_browser_capable(monkeypatch, capable=False)
    _patch_display(monkeypatch, available=False)
    _patch_chrome(monkeypatch, available=True)
    result = validate("browser_enabled", True)
    assert not result.ok


# ---------------------------------------------------------------------------
# chrome_binary
# ---------------------------------------------------------------------------


def test_chrome_binary_existing_path_ok(tmp_path: Path):
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh")
    result = validate("chrome_binary", str(fake))
    assert result.ok
    assert result.reason is None


def test_chrome_binary_missing_path_not_ok(tmp_path: Path):
    missing = tmp_path / "no_chrome_here"
    result = validate("chrome_binary", str(missing))
    assert not result.ok
    assert result.reason is not None
    assert "not found" in result.reason.lower()


# ---------------------------------------------------------------------------
# ops_concurrency
# ---------------------------------------------------------------------------


def test_ops_concurrency_boundary_1_ok():
    assert validate("ops_concurrency", 1).ok


def test_ops_concurrency_boundary_64_ok():
    assert validate("ops_concurrency", 64).ok


def test_ops_concurrency_zero_not_ok():
    result = validate("ops_concurrency", 0)
    assert not result.ok
    assert result.reason is not None
    assert "1" in result.reason and "64" in result.reason


def test_ops_concurrency_65_not_ok():
    result = validate("ops_concurrency", 65)
    assert not result.ok


def test_ops_concurrency_bool_true_not_ok():
    """True == 1 as int, but bools are rejected explicitly."""
    result = validate("ops_concurrency", True)
    assert not result.ok


def test_ops_concurrency_bool_false_not_ok():
    """False == 0 as int, but bools are rejected explicitly."""
    result = validate("ops_concurrency", False)
    assert not result.ok


def test_ops_concurrency_string_not_ok():
    result = validate("ops_concurrency", "3")
    assert not result.ok


def test_ops_concurrency_float_not_ok():
    result = validate("ops_concurrency", 3.5)
    assert not result.ok


# ---------------------------------------------------------------------------
# watchdog_interval_seconds
# ---------------------------------------------------------------------------


def test_watchdog_interval_s_positive_ok():
    assert validate("watchdog_interval_seconds", 30).ok
    assert validate("watchdog_interval_seconds", 0.5).ok


def test_watchdog_interval_s_zero_not_ok():
    result = validate("watchdog_interval_seconds", 0)
    assert not result.ok
    assert result.reason is not None


def test_watchdog_interval_s_negative_not_ok():
    result = validate("watchdog_interval_seconds", -1)
    assert not result.ok


# ---------------------------------------------------------------------------
# unregistered field (no-precondition case)
# ---------------------------------------------------------------------------


def test_machine_description_unregistered_always_ok():
    """machine_description has no validator entry; any value is accepted."""
    result = validate("machine_description", "anything")
    assert result.ok
    assert result.reason is None


def test_unknown_field_always_ok():
    result = validate("nonexistent_field_xyz", 42)
    assert result.ok


# ---------------------------------------------------------------------------
# read_time_capability
# ---------------------------------------------------------------------------


def test_read_time_capability_browser_enabled_returns_validation_result(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_browser_capable(monkeypatch, capable=True)
    result = read_time_capability("browser_enabled")
    assert isinstance(result, ValidationResult)


def test_read_time_capability_browser_enabled_not_ok_without_display(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_browser_capable(monkeypatch, capable=False)
    _patch_display(monkeypatch, available=False)
    _patch_chrome(monkeypatch, available=False)
    result = read_time_capability("browser_enabled")
    assert result is not None
    assert not result.ok


def test_read_time_capability_other_host_fields_return_none():
    """All other host fields have no static gate; validity depends on the value."""
    for field in (
        "chrome_binary",
        "ops_concurrency",
        "watchdog_interval_seconds",
        "machine_description",
        "some_unknown_field",
    ):
        assert read_time_capability(field) is None, f"expected None for {field!r}"
