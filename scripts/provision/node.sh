#!/usr/bin/env bash
# Node 22 — the version the frontend (Next.js) build + agent tooling target. One
# source for install.sh / Dockerfile / CI. Linux: nodesource (Ubuntu 24.04 ships
# 18); macOS: Homebrew. Idempotent.
#
# WSL without sudo: a host that already runs node >= 20.9 (the frontend's floor)
# skips apt entirely — the nodesource setup + apt install need root. An apt
# failure degrades to a warning: the gateway can run headless without the
# frontend (`ava start --disable-service frontend`).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

OS="$(prov_os)"
case "$OS" in
  linux)
    if command -v node >/dev/null 2>&1 \
        && [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge 20 ] 2>/dev/null; then
      prov_log "node $(node --version) already present — skipping apt (works without sudo)"
    elif ! prov_sudo apt-get update; then
      prov_log "WARNING apt-get update failed (no passwordless sudo?) — node not installed; the frontend needs it (run the gateway headless without it)"
    else
      prov_apt_install ca-certificates curl gnupg
      curl -fsSL https://deb.nodesource.com/setup_22.x | prov_sudo bash -
      prov_apt_install nodejs
    fi
    ;;
  macos)
    # The unversioned `node` formula is linked into /opt/homebrew, so npx lands
    # on PATH; `node@22` is keg-only (never symlinked) and would leave npx
    # unreachable for the ava-browser daemon. Prefer the linked formula, and
    # force-link the keg on the fallback path (QA review 2026-09-01, PR #1286 P1).
    brew install node 2>/dev/null \
        || { brew install node@22 2>/dev/null && brew link --force node@22; }
    ;;
esac
prov_log "node $(node --version 2>/dev/null || echo '(not on PATH)')"
