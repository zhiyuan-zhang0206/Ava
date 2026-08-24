#!/usr/bin/env bash
# Shared helpers for the scripts/provision/* pieces — one OS-dispatch + logging
# surface so the three consumers install the same way without duplicating package
# logic: `scripts/install.sh` (a unit), the `Dockerfile` (the eval image), and
# `install-system.sh` (a whole bare host). Each piece sources this, resolves the
# platform once, and branches.
#
# Source it from a sibling script:
#   . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"
#
# Supported platforms: Linux (Debian/Ubuntu/WSL, via apt), macOS (via brew),
# and Windows (via Docker — see docker-compose.windows.yml).
set -euo pipefail

# Provisioning must never upgrade formulae without explicit operator approval.
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1

prov_log() { echo "  [provision] $*"; }
prov_die() { echo "provision: $*" >&2; exit 2; }

# Echo the platform token (`linux` / `macos`); die on anything else. Call as
# `OS="$(prov_os)"` so a die inside the subshell propagates via set -e (a bare
# `case "$(prov_os)"` would swallow the exit).
prov_os() {
  case "$(uname -s)" in
    Linux)
      command -v apt-get >/dev/null 2>&1 \
        || prov_die "Linux without apt-get is unsupported (Debian/Ubuntu/WSL only)"
      echo linux
      ;;
    Darwin)
      command -v brew >/dev/null 2>&1 \
        || prov_die "macOS without Homebrew — install it first (https://brew.sh)"
      echo macos
      ;;
    CYGWIN*|MINGW*|MSYS*)
      echo windows
      ;;
    *)
      prov_die "unsupported platform $(uname -s) (Linux/Debian-Ubuntu/WSL, macOS, or Windows/Git-Bash)"
      ;;
  esac
}

# Run a command as root when needed: a non-root Linux user installing on a
# fresh box needs root for apt + keyring/apt-sources writes (Postgres refuses
# to run initdb as root, so the install itself must run non-root). Uses
# passwordless sudo (`sudo -n`); when sudo is absent or password-gated, runs
# the command bare so the callers' existing failure branches (warn + skip)
# keep degrading gracefully — exactly today's WSL-without-sudo behavior.
prov_sudo() {
    if [ "$(id -u)" != 0 ] \
        && command -v sudo >/dev/null 2>&1 \
        && sudo -n true 2>/dev/null; then
        sudo "$@"
    else
        "$@"
    fi
}

prov_apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  prov_sudo apt-get install -y --no-install-recommends "$@"
}
