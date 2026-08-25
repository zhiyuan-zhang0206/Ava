# Windows uv install pin

The uv standalone-install pin
([../2026-08-24/uv-standalone-install-pin.md](../2026-08-24/uv-standalone-install-pin.md))
covered `scripts/provision/toolchain.sh` (POSIX) and the CI setup-uv workflows,
but `conventions/windows-setup.md` still told a fresh native-Windows unit to
install uv by piping the astral installer — rolling latest, no checksum. The
Windows guide now downloads the pinned 0.10.2 release zip and refuses to
install when the sha256 does not match.

`shared/brew_pin.py` gains `UV_WINDOWS_ASSET_SHA256` (the zip assets) beside the
POSIX tarball map; the guide embeds the x86_64 value and names the ARM64 hash,
and the contract test (`tests/scripts/test_toolchain_uv_pin.py`) asserts the
guide never drifts — it also now covers `audit-branch-protection.yml`, the one
remaining setup-uv workflow outside the contract.
