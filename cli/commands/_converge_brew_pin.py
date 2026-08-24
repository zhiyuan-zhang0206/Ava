"""Warning-only converge assertion for the approved Homebrew pin set."""

from __future__ import annotations

import sys

from cli.commands._converge_spec import ConvergeCtx
from shared.brew_pin import unpinned_formulae
from shared.platform import IS_MACOS


def ensure_brew_pin(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Warn when an approved formula is unpinned; never repair or block start."""
    if not IS_MACOS:
        return
    missing = unpinned_formulae()
    if not missing:
        return
    commands = ", ".join(f"`brew pin {formula}`" for formula in missing)
    print(
        f"  ! brew-pin: unpinned formulae: {', '.join(missing)}; re-pin manually with {commands}",
        file=sys.stderr,
    )
