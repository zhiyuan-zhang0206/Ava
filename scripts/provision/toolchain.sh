#!/usr/bin/env bash
# uv — the package manager every Ava unit and the verification image use. Installed
# from a pinned GitHub release asset (fixed version + sha256, single source
# with base/host/brew_pin.py) instead of the astral installer's rolling latest,
# so a fresh box gets the same operator-approved version CI and brew-pinned
# hosts run. Idempotent: skips the fetch only when that exact `uv` version is on PATH.
#
# This installs ONLY the uv binary. Interpreter provisioning + dependency sync
# differ per consumer and stay with them: source development uses its locked
# dependency installer; the verification image builds its venv at run time from the
# commit under test's lockfile.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

# Pinned uv release — must match base/host/brew_pin.py UV_VERSION / UV_ASSET_SHA256
# (contract test: tests/scripts/test_toolchain_uv_pin.py).
UV_VERSION="0.12.23"
# Overridable so a mirror user can point at a GitHub proxy they trust; the
# sha256 check below keeps the pin regardless of where the asset came from.
UV_RELEASE_BASE_URL="${UV_RELEASE_BASE_URL:-https://github.com/astral-sh/uv/releases/download}"

# Map this machine to a release asset: prints "<platform-tag> <sha256>".
uv_asset() {
  local machine
  machine="$(uname -m)"
  case "$(uname -s)-${machine}" in
    Darwin-arm64|Darwin-aarch64)
      echo "aarch64-apple-darwin 50487ae565ccd96e499056b4674d438f4c53170202617b4c759defe0c6a1b544" ;;
    Darwin-x86_64|Darwin-amd64)
      echo "x86_64-apple-darwin 960da44cb4b73685206ddd250b19e0a117fa41095710c1038f081f5cb613efb4" ;;
    Linux-x86_64|Linux-amd64)
      echo "x86_64-unknown-linux-gnu 9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6" ;;
    Linux-arm64|Linux-aarch64)
      echo "aarch64-unknown-linux-gnu 6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f" ;;
    *)
      prov_die "no pinned uv ${UV_VERSION} asset for $(uname -s)-${machine}" ;;
  esac
}

install_uv() {
  local tag expected actual archive tmp
  read -r tag expected <<< "$(uv_asset)"
  tmp="$(mktemp -d)"
  archive="${tmp}/uv-${tag}.tar.gz"
  prov_log "downloading uv ${UV_VERSION} (${tag})"
  curl -LsSf --retry 5 --retry-all-errors -o "${archive}" \
    "${UV_RELEASE_BASE_URL}/${UV_VERSION}/uv-${tag}.tar.gz" \
    || prov_die "failed to download uv ${UV_VERSION} from ${UV_RELEASE_BASE_URL}/${UV_VERSION}/uv-${tag}.tar.gz"
  if command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "${archive}" | awk '{print $1}')"
  elif command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "${archive}" | awk '{print $1}')"
  else
    prov_die "no sha256 tool (shasum/sha256sum) available to verify the uv download"
  fi
  if [ "${actual}" != "${expected}" ]; then
    prov_die "uv ${UV_VERSION} sha256 mismatch: got ${actual}, expected ${expected} — refusing to install"
  fi
  tar -xzf "${archive}" -C "${tmp}"
  mkdir -p "${HOME}/.local/bin"
  install -m 0755 "${tmp}/uv-${tag}/uv" "${HOME}/.local/bin/uv"
  rm -rf "${tmp}"
  prov_log "uv ${UV_VERSION} installed to ~/.local/bin"
}

# "Already present" has to mean WORKS, not exists. A 0-byte file with the exec
# bit set is run by the shell as an empty script — no output, exit 0 — so
# `command -v uv` finds it, the install is skipped, and every later `uv run
# <anything>` silently succeeds without doing the thing. That is exactly how the
# CI runner snapshot shipped a do-nothing uv and 36 hours of green CI ran zero
# tests. Version output is the discriminator: an empty file can fake an exit
# code, it cannot fake stdout — and an answer of the wrong version is the same
# class of lie, so that dies too.
if uv --version 2>/dev/null | grep -q .; then
  uv_actual="$(uv --version | awk '{print $2}')"
  if [ "${uv_actual}" != "${UV_VERSION}" ]; then
    prov_die "uv ${uv_actual} is already on PATH; the pin installs ${UV_VERSION} — replace the binary before provisioning"
  fi
  prov_log "uv already present ($(uv --version))"
else
  install_uv
fi
