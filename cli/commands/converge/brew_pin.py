"""Warning-only converge assertion for the approved Homebrew pin set."""

from __future__ import annotations

import sys

from base.host.brew_pin import unpinned_formulae
from cli.commands.converge.spec import ConvergeCtx


def ensure_brew_pin(ctx: ConvergeCtx) -> None:
    """Warn when an approved formula is unpinned; never repair or block start."""
    if ctx.host.platform.system != "darwin":
        return
    missing = unpinned_formulae(run=ctx.host.run)
    if not missing:
        return
    commands = ", ".join(f"`brew pin {formula}`" for formula in missing)
    print(
        f"  ! brew-pin: unpinned formulae: {', '.join(missing)}; re-pin manually with {commands}",
        file=sys.stderr,
    )
