#!/usr/bin/env bash
# uv — the package manager every Ava unit and the eval image use. The astral
# installer ships a static binary and is cross-platform, so there is no OS branch.
# Idempotent: skips the fetch when `uv` is already on PATH.
#
# This installs ONLY the uv binary. Interpreter provisioning + dependency sync
# differ per consumer and stay with them: a unit runs `uv python install 3.12` +
# `uv sync` in install.sh; the eval image bakes its venv in a dedicated Dockerfile
# layer (it needs the pyproject/uv.lock COPY + the corp-CA build secret).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

# "Already present" has to mean WORKS, not exists. A 0-byte file with the exec
# bit set is run by the shell as an empty script — no output, exit 0 — so
# `command -v uv` finds it, the install is skipped, and every later `uv run
# <anything>` silently succeeds without doing the thing. That is exactly how the
# CI runner snapshot shipped a do-nothing uv and 36 hours of green CI ran zero
# tests. Version output is the discriminator: an empty file can fake an exit
# code, it cannot fake stdout.
if uv --version 2>/dev/null | grep -q .; then
  prov_log "uv already present ($(uv --version))"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  prov_log "uv installed to ~/.local/bin"
fi
