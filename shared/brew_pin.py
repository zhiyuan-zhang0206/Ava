"""Read-only assertion helpers for the operator-approved Homebrew pin set."""

from __future__ import annotations

import subprocess

PINNED_BREW_FORMULAE: frozenset[str] = frozenset(
    {
        "ca-certificates",
        "cloudflared",
        "grafana",
        "json-c",
        "node",
        "openssl@3",
        "pgbouncer",
        "postgresql@17",
        "redis",
        "redis@8.2",
        "tailscale",
        "uv",
    }
)


def pinned_brew_formulae() -> set[str] | None:
    """Return Homebrew's pinned formulae, or ``None`` when brew is absent.

    This probe is warning-only infrastructure: process errors degrade to an
    empty observed set so callers can report drift without breaking lifecycle
    commands. It never mutates pins and does no work at import time.
    """
    try:
        result = subprocess.run(
            ["brew", "list", "--pinned"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def unpinned_formulae() -> tuple[str, ...]:
    """Return approved formulae missing from the host's pin set, sorted."""
    pinned = pinned_brew_formulae()
    if pinned is None:
        return ()
    return tuple(sorted(PINNED_BREW_FORMULAE - pinned))
