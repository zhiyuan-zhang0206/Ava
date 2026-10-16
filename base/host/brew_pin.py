from __future__ import annotations

import subprocess
from pathlib import Path

from base.host.proc import CommandRunner

_BREW_TIMEOUT_S = 60.0

# ava:ref base/host/docs/brew_pin.ava.okf.md#brew-formula-manifest
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

# ava:ref base/host/docs/brew_pin.ava.okf.md#uv-pin-copies
UV_VERSION = "0.13.1"

UV_ASSET_SHA256: dict[str, str] = {
    "aarch64-apple-darwin": "cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2",
    "x86_64-apple-darwin": "bb7b805bdcf3d0bc8c74ceb94569e6b3e8aa35bd492acdbb9d46bf46d9d4ff6b",
    "aarch64-unknown-linux-gnu": "9041c73c99fdc5892cf128140beb354eca9231d502068cc36e6cb53a8e59ad14",
    "x86_64-unknown-linux-gnu": "eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220",
}

# ava:ref base/host/docs/brew_pin.ava.okf.md#apt-pin-copies
PGBOUNCER_APT_VERSION = "1.26.0-1.pgdg24.04+1"

REDIS_APT_VERSION = "6:8.2.*"


# ava:ref base/host/docs/brew_pin.ava.okf.md#brew-probe-warnings
def pinned_brew_formulae(*, run: CommandRunner) -> set[str] | None:
    try:
        result = run(
            ["brew", "list", "--pinned"], capture_output=True, text=True, timeout=_BREW_TIMEOUT_S
        )
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def installed_brew_formulae(*, run: CommandRunner) -> set[str] | None:
    try:
        result = run(
            ["brew", "list", "--formula"], capture_output=True, text=True, timeout=_BREW_TIMEOUT_S
        )
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def unpinned_formulae(*, run: CommandRunner) -> tuple[str, ...]:
    pinned = pinned_brew_formulae(run=run)
    if pinned is None:
        return ()
    installed = installed_brew_formulae(run=run)
    if installed is None:
        return ()
    return tuple(sorted((PINNED_BREW_FORMULAE & installed) - pinned))


def brew_prefix(formula: str = "") -> Path:
    cmd = ["brew", "--prefix", *([formula] if formula else [])]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return Path("/opt/homebrew")
    return Path(out.stdout.strip() or "/opt/homebrew")
