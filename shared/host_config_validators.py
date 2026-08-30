"""Host-side capability validators for remote host-config writes.

The gateway cannot know a remote host's local reality, so a host-config
write is a request the receiving host may reject with a reason.  This module is
that host-side judgment: a registry of per-field validators that check whether
a proposed value is actually applicable on this machine.

Layering: lives in ``shared`` (lowest import layer).  The Chrome / display
probes come from ``shared.platform_probes`` (the single source of truth shared
with the browser daemon and the MCP config loader).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shared.platform_probes import browser_capable, display_available, resolve_chrome_binary


@dataclass(frozen=True)
class ValidationResult:
    """Result of a field-level capability check.

    ``ok`` is True when the proposed value is acceptable on this host.
    ``reason`` is a human-readable explanation shown in the UI; it is ``None``
    if and only if ``ok`` is True.
    """

    ok: bool
    reason: str | None = None  # human-readable, shown in UI; None iff ok


# Per-field validator functions


def _validate_browser_enabled(value: object) -> ValidationResult:
    """Gate the ``True`` value: display + Chrome binary + npx must all be present.

    Uses the shared ``browser_capable()`` predicate (same gate as
    ``_services_for_roles``, watchdog ``_checks_for_capability``, and
    ``agent/warmup.py``). ``False`` is always ok.
    """
    if not value:
        return ValidationResult(ok=True)
    if browser_capable():
        return ValidationResult(ok=True)
    # Give the most specific reason by probing individual preconditions.
    if not display_available():
        return ValidationResult(ok=False, reason="no display detected")
    chrome = resolve_chrome_binary()
    if chrome is None or not Path(chrome).exists():
        return ValidationResult(ok=False, reason="Chrome binary not found")
    return ValidationResult(ok=False, reason="npx not found (Node.js required)")


def _validate_chrome_binary(value: object) -> ValidationResult:
    """The given path must exist on this host."""
    p = Path(str(value))
    if p.exists():
        return ValidationResult(ok=True)
    return ValidationResult(ok=False, reason=f"path not found: {p}")


def _validate_ops_concurrency(value: object) -> ValidationResult:
    """Must be an int in [1, 64].  Bools (subclass of int) and non-ints are rejected."""
    # isinstance(True, int) is True in Python — reject bools explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        return ValidationResult(ok=False, reason="must be an integer in [1, 64]")
    if 1 <= value <= 64:
        return ValidationResult(ok=True)
    return ValidationResult(ok=False, reason="must be an integer in [1, 64]")


def _validate_watchdog_interval_seconds(value: object) -> ValidationResult:
    """Must be a positive number."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ValidationResult(ok=False, reason="must be > 0")
    if value > 0:
        return ValidationResult(ok=True)
    return ValidationResult(ok=False, reason="must be > 0")


_TRANSFER_BACKENDS = frozenset({"drive", "none"})


def _validate_cross_machine_transfer_backend(value: object) -> ValidationResult:
    """A closed set of backends — reject anything else rather than let a typo
    land in .env (fail-fast at write time, not at the next settings load)."""
    if value in _TRANSFER_BACKENDS:
        return ValidationResult(ok=True)
    known = ", ".join(sorted(_TRANSFER_BACKENDS))
    return ValidationResult(ok=False, reason=f"must be one of: {known}")


# Registry

# Maps a host field name to its validator.  Only fields with a real local
# precondition have an entry; absent -> always ok (see ``validate`` below).
VALIDATORS: dict[str, Callable[[object], ValidationResult]] = {
    "browser_enabled": _validate_browser_enabled,
    "chrome_binary": _validate_chrome_binary,
    "ops_concurrency": _validate_ops_concurrency,
    "watchdog_interval_seconds": _validate_watchdog_interval_seconds,
    "cross_machine_transfer_backend": _validate_cross_machine_transfer_backend,
    # "machine_description" has no entry: free text, always ok.
}


# Public API


def validate(field_name: str, value: object) -> ValidationResult:
    """Validate a proposed value for a host config field on this machine.

    Looks up ``field_name`` in ``VALIDATORS``.  If no validator is registered
    the result is always ok — this is the designed no-precondition case (e.g.
    free-text fields like ``machine_description``), not a fail-fast violation.

    Seam 4 (runner handler) calls this before writing a field.
    """
    validator = VALIDATORS.get(field_name)
    if validator is None:
        return ValidationResult(ok=True)
    return validator(value)


def read_time_capability(field_name: str) -> ValidationResult | None:
    """Return a static pre-value capability verdict for ``field_name``, or None.

    A read-time gate applies to fields where the "on" state may be statically
    impossible before the user picks a value.  Today only ``browser_enabled``
    qualifies: ``True`` may be un-enableable on a headless host, so the UI can
    pre-grey the toggle.

    All other fields have no static gate — their validity depends on the
    user-supplied value and is checked at write time only.  Seam 6 uses the
    non-None return to pre-grey impossible toggles in the config UI.
    """
    if field_name == "browser_enabled":
        # FBT003: True here is the candidate value being validated, not a
        # boolean-trap flag argument.
        return validate("browser_enabled", True)  # noqa: FBT003
    return None
