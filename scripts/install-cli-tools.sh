#!/usr/bin/env bash
# Single install entry for system CLI tools — the main Dockerfile RUNs it.
#
# Installs: apt baseline (gnupg / ca-cert / curl) + GitHub CLI keyring +
# high-frequency CLI tools used by the agent (git / rg / jq / fd / bat /
# tree / fzf / htop / gh).
#
# Does NOT touch: nodejs / uv / Python deps / playwright / mcp-language-server
# — those are strongly tied to base image and image purpose (main Dockerfile
# uses nodesource node 22 + astral uv installer + playwright; eval-bench
# uses Debian default node + pip install uv + no playwright). Each
# Dockerfile installs its own.
#
# Does NOT clean /var/lib/apt/lists/* — caller decides (main Dockerfile
# uses BuildKit cache mount and skips cleanup; eval-bench install.sh cleans
# to save image size).
#
# WSL without sudo: a host that already carries every tool skips apt entirely
# (the gh keyring + apt-sources writes below need root), and an apt failure
# degrades to a warning instead of a hard stop — the missing tools can be
# installed later via `sudo apt-get install ...`. See conventions/windows-setup.md.

set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/provision/_lib.sh"

# The complete tool set, keyed by the binary each package provides (Debian
# renames fd-find -> fdfind and bat -> batcat). A host that already carries
# every one of them — a WSL distro without sudo, a pre-provisioned CI image —
# skips apt entirely.
TOOL_BINS=(curl gpg git rg jq fdfind batcat tree fzf htop gh)
missing=()
for bin in "${TOOL_BINS[@]}"; do
    command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
done
if [ ${#missing[@]} -eq 0 ]; then
    echo "install-cli-tools.sh: all CLI tools already present — skipping apt (works without sudo)"
    exit 0
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "install-cli-tools.sh: requires apt-get (Debian/Ubuntu/WSL)." >&2
    echo "  macOS: brew install git ripgrep jq fd bat tree fzf htop gh" >&2
    echo "  Windows: install these via winget or use WSL2 (see conventions/windows-setup.md)" >&2
    exit 2
fi

export DEBIAN_FRONTEND=noninteractive

if ! prov_sudo apt-get update; then
    echo "install-cli-tools.sh: WARNING apt-get update failed (no passwordless sudo?) — continuing without installing: ${missing[*]}" >&2
    echo "  Install them later with: sudo apt-get install -y ${missing[*]}" >&2
    exit 0
fi
prov_sudo apt-get install -y --no-install-recommends \
    gnupg ca-certificates curl

# GitHub CLI keyring + repo (gh is not in Debian/Ubuntu default sources).
# Only touched when gh is absent — a present gh must not force root writes.
# Best-effort: on a WSL distro without sudo the keyring write fails, and gh
# then comes from another source or must be installed manually.
if ! command -v gh >/dev/null 2>&1; then
    if ! curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
            | gpg --dearmor 2>/dev/null \
            | prov_sudo tee /usr/share/keyrings/githubcli-archive-keyring.gpg > /dev/null; then
        echo "install-cli-tools.sh: WARNING could not add the GitHub CLI apt repo (keyring write needs root) — gh must be installed manually" >&2
    else
        echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
            | prov_sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        prov_sudo apt-get update
    fi
fi

prov_sudo apt-get install -y --no-install-recommends \
    git ripgrep jq \
    fd-find bat tree fzf htop gh
