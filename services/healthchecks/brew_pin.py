"""Watchdog assertion for drift from the operator-approved Homebrew pin set."""

from __future__ import annotations

import logging

from shared.brew_pin import unpinned_formulae
from shared.log import init_gateway_process
from shared.platform import IS_MACOS

_log = logging.getLogger("services.healthchecks.brew_pin")

# Reporting state only: a persistent drift episode should not emit an ERROR on
# every watchdog round. A healthy round clears it so recurrence is loud again.
_reported_missing: tuple[str, ...] = ()


def main() -> None:
    global _reported_missing  # noqa: PLW0603 — state intentionally spans watchdog rounds

    init_gateway_process(name="brew_pin-healthcheck")
    if not IS_MACOS:
        return

    missing = unpinned_formulae()
    if not missing:
        _reported_missing = ()
        return
    if missing == _reported_missing:
        return

    _reported_missing = missing
    commands = ", ".join(f"brew pin {formula}" for formula in missing)
    _log.error(
        "[brew-pin healthcheck] unpinned Homebrew formulae: %s; re-pin manually with %s",
        ", ".join(missing),
        commands,
    )


if __name__ == "__main__":
    main()
