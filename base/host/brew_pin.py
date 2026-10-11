"""Read-only warning helpers for installed operator-approved Homebrew pins."""

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
        "pgvector",
        "postgresql@17",
        "redis",
        "redis@8.2",
        "uv",
    }
)

# The operator-approved uv version for standalone installs (toolchain.sh on
# Linux/WSL/Docker, CI setup-uv input). Homebrew hosts pin the `uv` formula
# above; this is the same version for the GitHub-release-asset path.
# toolchain.sh embeds these values because it runs before Python exists on a
# fresh box; tests/scripts/test_toolchain_uv_pin.py asserts the copies match.
UV_VERSION = "0.12.23"

# SHA256 of each supported platform's release tarball (astral-sh/uv 0.12.23
# release asset digests, verified against all four downloaded archives). Keys
# are the asset-name platform suffix; toolchain.sh maps `uname` output to the
# same keys.
UV_ASSET_SHA256: dict[str, str] = {
    "aarch64-apple-darwin": "50487ae565ccd96e499056b4674d438f4c53170202617b4c759defe0c6a1b544",
    "x86_64-apple-darwin": "960da44cb4b73685206ddd250b19e0a117fa41095710c1038f081f5cb613efb4",
    "aarch64-unknown-linux-gnu": "6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f",
    "x86_64-unknown-linux-gnu": "9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6",
}


# The operator-approved PgBouncer build for Linux apt installs (pgdg, Ubuntu
# 24.04). Homebrew hosts pin the `pgbouncer` formula above. PgBouncer releases
# change pooled-session semantics (1.26 tracks `default_transaction_read_only`
# per client), so moving this needs the maintainer's approval.
# scripts/provision/database.sh (bash, before Python exists) and the CI install
# action embed the same string; tests/ci/test_pgbouncer_pin.py asserts they match.
PGBOUNCER_APT_VERSION = "1.26.0-1.pgdg24.04+1"

# The operator-approved Redis series for Linux apt installs (redis.io, Ubuntu
# 24.04): the newest 8.2 patch release, as the `redis@8.2` formula above is for
# Homebrew hosts. An apt glob, not one build, because the series is what the
# operator approved. scripts/provision/database.sh (bash, before Python exists)
# embeds the same string; tests/ci/test_redis_pin.py asserts the copies match.
REDIS_APT_VERSION = "6:8.2.*"


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


def installed_brew_formulae() -> set[str] | None:
    """Return Homebrew's installed formulae, or ``None`` when brew is absent.

    This probe is warning-only infrastructure: process errors degrade to an
    empty observed set so callers can report drift without breaking lifecycle
    commands. It never mutates pins and does no work at import time.
    """
    try:
        result = subprocess.run(
            ["brew", "list", "--formula"],
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
    """Return installed approved formulae missing from the host's pin set, sorted."""
    pinned = pinned_brew_formulae()
    if pinned is None:
        return ()
    installed = installed_brew_formulae()
    if installed is None:
        return ()
    return tuple(sorted((PINNED_BREW_FORMULAE & installed) - pinned))
