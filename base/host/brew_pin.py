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
UV_VERSION = "0.10.2"

# SHA256 of each supported platform's release tarball (astral-sh/uv 0.10.2
# per-asset .sha256 files, e.g. uv-aarch64-apple-darwin.tar.gz.sha256). Keys
# are the asset-name platform suffix; toolchain.sh maps `uname` output to the
# same keys.
UV_ASSET_SHA256: dict[str, str] = {
    "aarch64-apple-darwin": "3828b2de196687f60e9d199aea8b504299629300831eea0935ff3fe339903d0a",
    "x86_64-apple-darwin": "3cdbd038333cfe861ce04f3d91678547bf2e726224acf5f42d3f0affa6740e19",
    "aarch64-unknown-linux-gnu": "4998f545234d52fc6f1280827d392f00a9278295050d59c53a776546dbf0124d",
    "x86_64-unknown-linux-gnu": "6aa4576c31f791c0b9d4739e256d07358d45e7535695287fec03cf6839e25512",
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
